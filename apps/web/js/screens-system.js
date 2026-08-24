/* Fulcrum Ops — SYSTEM screens: Alerts, Exports, Licensing & Entitlements,
 * and Workspace Settings (profile + API access tokens).
 *
 * Everything on these three screens is read from the control plane. An alert
 * exists because something raised it, an export row exists because a file was
 * generated, a seat exists because somebody was given one. Nothing here is
 * simulated: when the workspace has no data the screen says so, and when a call
 * fails it shows the server's own message with a retry.
 */
(function(){
  'use strict';
  window.SCREENS = window.SCREENS || {};
  const { esc, relTime, fmtDate, fmtDateTime, fmtFull, fmtDur, barPct, hbars } = U;
  const { badge, statusText, ownerCell, avatarHtml, entityCell, kpiRow, kpiSkeleton, dataTable, pageHead,
          searchBox, tabBar, inspSection, kv, toast, openModal, confirmModal, screenError, openMenu } = C;

  /* ---------------- null-safe formatters ----------------
   * A value the server did not send is a dash, never a zero. */
  const dash = '<span class="faint">—</span>';
  const num = (v) => v == null ? dash : fmtFull(v);
  const pct = (v, d) => v == null ? dash : Number(v).toFixed(d == null ? 1 : d) + '%';
  const ts = (v) => v ? new Date(v).getTime() : null;
  const when = (v) => v ? `<span class="dim nowrap">${relTime(ts(v))}</span>` : dash;
  const at = (v) => v ? fmtDateTime(ts(v)) : dash;
  /**
   * A deadline, said as the distance still to run.
   *
   * `relTime` only speaks about the past — handed a future instant it answers
   * "just now", which would make a retention window a week out read as expired.
   */
  function until(v){
    if(!v) return dash;
    const delta = ts(v) - Date.now();
    if(delta <= 0) return relTime(ts(v));
    const mins = Math.round(delta / 60000);
    if(mins < 60) return `in ${Math.max(1, mins)} min${mins === 1 ? '' : 's'}`;
    const hours = Math.round(delta / 3600000);
    if(hours < 48) return `in ${hours} hour${hours === 1 ? '' : 's'}`;
    const days = Math.round(delta / 86400000);
    return `in ${days} day${days === 1 ? '' : 's'}`;
  }
  const day = (v) => v ? fmtDate(ts(v)) : dash;
  const dur = (v) => v == null ? dash : fmtDur(v);

  /** Bytes as the server counted them — no rounding up to a friendlier number. */
  function bytes(n){
    if(n == null) return dash;
    const v = Number(n);
    if(v < 1024) return v + ' B';
    if(v < 1048576) return (v/1024).toFixed(1).replace(/\.0$/,'') + ' KB';
    if(v < 1073741824) return (v/1048576).toFixed(1).replace(/\.0$/,'') + ' MB';
    return (v/1073741824).toFixed(2) + ' GB';
  }

  /** Money the server sent as a decimal string; never re-derived. */
  function money(v, dec){
    if(v == null || v === '') return dash;
    const n = Number(v);
    if(!isFinite(n)) return esc(String(v));
    return '$' + n.toLocaleString('en-US', { minimumFractionDigits: dec == null ? 2 : dec,
                                             maximumFractionDigits: dec == null ? 2 : dec });
  }

  /**
   * What a failed call should say.
   *
   * A 422 answers with a generic headline and puts the real reason in
   * `details.fields`; showing only the headline tells the reader nothing they
   * can act on, so the field messages win when the server sent any.
   */
  function errText(err){
    const fields = (err && err.details && err.details.fields) || [];
    if(fields.length) return fields.map(f => f.message.replace(/^Value error,\s*/, '')).join(' · ');
    return (err && err.message) || 'The request failed.';
  }

  /** Disable a control the signed-in role may not use, and say why on hover. */
  function gate(role, why){
    return Store.session.can(role) ? '' : ` disabled title="${esc(why)}"`;
  }
  /** The same check for a row-action handler, which cannot render disabled. */
  function allowed(role, why){
    if(Store.session.can(role)) return true;
    toast('error', 'Not permitted', why);
    return false;
  }

  /** An empty-state block that says what is actually missing. */
  function emptyBlock(icon, title, body){
    return `<div class="empty-state">${ICONS[icon] || ICONS.search}
      <div class="es-title">${esc(title)}</div><div>${esc(body || '')}</div></div>`;
  }

  /**
   * The workspace's members, fetched once per screen.
   *
   * Alerts and exports carry user ids, not names; this resolves them so a cell
   * can show a person. A member who cannot be resolved stays an id rather than
   * becoming an invented name.
   */
  function memberDirectory(){
    let byId = null, inflight = null, rows = [], loadErr = null;
    return {
      load(){
        if(byId) return Promise.resolve(rows);
        if(!inflight){
          // The directory is a bare {id, full_name, initials} array, not a page.
          inflight = API.alerts.assignees()
            .then(list => {
              rows = list || [];
              byId = {};
              rows.forEach(u => { byId[u.id] = u; });
              return rows;
            })
            // A refusal is not an empty workspace — remember it so callers can say why.
            .catch(err => { loadErr = err; byId = {}; rows = []; return rows; });
        }
        return inflight;
      },
      error(){ return loadErr; },
      items(){ return rows; },
      user(id){ return (byId && byId[id]) || null; },
      name(id){
        if(!id) return null;
        const u = byId && byId[id];
        return u ? u.full_name : null;
      },
      /** A person cell, or the raw id when the directory has not resolved it. */
      cell(id, fallback){
        if(!id) return fallback || dash;
        const u = byId && byId[id];
        if(!u) return `<span class="mono dim" title="${loadErr ? 'Member names could not be loaded' : 'This user is no longer a member of the workspace'}">${esc(String(id).slice(0,8))}…</span>`;
        return ownerCell(u.full_name, '');
      },
    };
  }

  /* ================= ALERTS ================= */
  const AL_KPIS = ['Open Alerts','Critical','Investigating','Acknowledged','MTTA'];
  const AL_SEVERITIES = ['Critical','High','Medium','Low','Info'];
  const AL_STATUS = ['Open','Investigating','Acknowledged','Resolved','Muted'];
  const AL_CHANNELS = ['Teams','Email','PagerDuty','Slack','Webhook'];

  const sevColor = (s) => s === 'Critical' || s === 'High' ? 'red' : s === 'Medium' ? 'amber' : s === 'Low' ? 'green' : 'blue';
  const alStatusColor = (s) => s === 'Open' ? 'red' : s === 'Investigating' ? 'amber'
    : s === 'Resolved' ? 'green' : s === 'Muted' ? 'gray' : 'blue';

  SCREENS['alerts'] = {
    title:'Alerts',
    render(main){
      let selectedId = null, selectedRow = null;
      const members = memberDirectory();

      main.innerHTML = `
        ${pageHead({title:'Alerts', sub:'Operational alerts across connections, policies, quotas, secrets, tests, and deployments.',
          actions:`${searchBox('alSearch','Search alerts…')}
          <button class="btn" id="alExport">${ICONS.download}Export</button>
          <button class="btn" id="alRules">${ICONS.sliders}Alert Rules</button>
          <button class="btn primary" id="alAckAll"${gate('operator','Acknowledging alerts requires the operator role.')}>${ICONS.checkCircle}Acknowledge All</button>`})}
        <div id="alKpis">${kpiSkeleton(AL_KPIS)}</div>
        <div class="with-inspector mt" id="alLayout">
          <div id="alTableWrap"></div>
          <div class="inspector" id="alInspector"></div>
        </div>`;

      // ---- KPI cards ----------------------------------------------------
      function loadSummary(){
        const host = document.getElementById('alKpis');
        if(!host) return;
        host.innerHTML = kpiSkeleton(AL_KPIS);
        API.alerts.summary()
          .then(s => {
            if(!document.getElementById('alKpis')) return;
            host.innerHTML = kpiRow([
              { label:'Open Alerts', value:fmtFull(s.open), icon:'bell', color:'red',
                sub: s.total ? `${fmtFull(s.total)} in the queue` : 'Nothing in the queue' },
              { label:'Critical', value:fmtFull(s.critical), icon:'alert', color:'red',
                sub: s.critical ? 'Requires immediate action' : 'None outstanding' },
              { label:'Investigating', value:fmtFull(s.investigating), icon:'search', color:'amber',
                sub: s.muted ? `${fmtFull(s.muted)} muted` : 'Being worked' },
              { label:'Acknowledged', value:fmtFull(s.acknowledged), icon:'checkCircle', color:'blue',
                sub: `${fmtFull(s.resolved_24h)} resolved in 24h` },
              // The card is time-to-acknowledge; the line under it is time-to-resolve,
              // so each half says which of the two the server has not measured yet.
              { label:'MTTA', value: s.mtta_seconds == null ? '—' : `<span style="font-size:20px">${esc(fmtDur(s.mtta_seconds))}</span>`,
                icon:'clock', color:'green',
                sub: s.mttr_seconds != null ? `MTTR ${fmtDur(s.mttr_seconds)}`
                  : s.mtta_seconds == null ? 'No alert has been acknowledged yet'
                  : 'No alert has been resolved yet' },
            ]);
            fillSources(s.sources || []);
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSummary, 'the alert summary')); });
      }

      /** The Source dropdown lists the screens that actually raised something. */
      function fillSources(sources){
        const sel = table.filterEl && table.filterEl.querySelector('[data-fi="2"]');
        if(!sel) return;
        const have = new Set(Array.from(sel.options).map(o => o.value || o.textContent));
        sources.forEach(s => {
          if(have.has(s)) return;
          have.add(s);
          const o = document.createElement('option');
          o.textContent = s;
          sel.appendChild(o);
        });
      }

      // ---- the triage queue -----------------------------------------------
      const table = dataTable({
        columns:[
          { key:'severity', label:'Severity', render:r => badge(r.severity, sevColor(r.severity)) },
          { key:'title', label:'Alert', render:r => `<div><div class="cell-main">${esc(r.title)}</div>
              <div class="cell-sub">${esc((r.description || '').length > 72 ? r.description.slice(0,72) + '…' : (r.description || '—'))}</div></div>` },
          { key:'source', label:'Source', render:r => `<span class="badge bg-gray">${esc(r.source)}</span>` },
          { key:'status', label:'Status', render:r => statusText(r.status, alStatusColor(r.status))
              + (r.occurrence_count > 1 ? ` <span class="badge bg-gray" title="Recurrences folded onto this alert">×${r.occurrence_count}</span>` : '') },
          { key:'raised_at', label:'Time', render:r => when(r.raised_at) },
        ],
        rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'alerts',
        searchPlaceholder:'Search alerts…',
        defaultSort:{ key:'raised_at', dir:-1 },
        emptyText:'No alerts have been raised in this workspace',
        filters:[
          { key:'severity', label:'Severity', param:'severity', options:AL_SEVERITIES, allLabel:'All Severities' },
          { key:'status', label:'Status', param:'status', options:AL_STATUS, allLabel:'All Status' },
          { key:'source', label:'Source', param:'source', options:[], allLabel:'All Sources' },
        ],
        source: (params) => API.alerts.list(params),
        exportSource: (params) => API.alerts.export(params),
        autoSelectFirst: true,
        onSelect: showAlert,
        rowActions: r => [
          { label:'Acknowledge', icon:'check', onClick:()=>acknowledge(r) },
          { label:'Resolve', icon:'checkCircle', onClick:()=>resolve(r) },
          { label:'Assign…', icon:'users', onClick:()=>openAssign(r) },
          { label:'Open Source Screen', icon:'external', onClick:()=>APP.go(APP.sourceRoute(r.source)) },
          { sep:true },
          // A muted alert offers the way back; anything else offers the mute.
          ...(r.status === 'Muted'
            ? [{ label:'Unmute', icon:'bell', onClick:()=>unmute(r) }]
            : [{ label:'Mute for 24h', icon:'clock', onClick:()=>mute(r, 1440) }]),
        ],
      });

      const wrap = document.getElementById('alTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);
      document.getElementById('alSearch').addEventListener('input', e => table.search(e.target.value));
      document.getElementById('alExport').addEventListener('click', () => table.export());

      /* Only the inspector names people, so the directory arriving late repaints
         that panel rather than re-asking the server for the whole page of rows. */
      members.load().then(() => { if(selectedRow) showAlert(selectedRow); });
      loadSummary();

      /** Everything an alert action changes: the row, the cards and the badge. */
      function afterChange(){
        table.refresh();
        loadSummary();
        Store.refreshBadges();
      }

      // ---- actions ---------------------------------------------------------
      async function acknowledge(r){
        if(!allowed('operator','Acknowledging an alert requires the operator role.')) return;
        try {
          const res = await Store.mutate(() => API.alerts.acknowledge(r.id), { event:'alerts:changed' });
          toast('success','Acknowledged', res.message || r.title);
          afterChange();
          if(selectedId === r.id) showAlert(r);
        } catch (err) {
          toast('error','Could not acknowledge', errText(err));
        }
      }

      function resolve(r){
        if(!allowed('operator','Resolving an alert requires the operator role.')) return;
        openModal({
          title:'Resolve Alert', icon:'checkCircle',
          body:`<div class="quote">${esc(r.title)}</div>
            <div class="form-row mt"><label>CLOSING NOTE (OPTIONAL)</label>
              <input class="input" id="alResNote" placeholder="What fixed it?"></div>`,
          footer:[
            { label:'Cancel' },
            { label:'Resolve', cls:'primary', onClick: async (close, modal) => {
                const note = modal.querySelector('#alResNote').value.trim();
                close();
                try {
                  const res = await Store.mutate(() => API.alerts.resolve(r.id, note ? { resolution_note: note } : {}),
                    { event:'alerts:changed' });
                  const mttr = res.data && res.data.mttr_seconds;
                  toast('success','Resolved', mttr == null ? (res.message || r.title) : `${r.title} — resolved in ${fmtDur(mttr)}.`);
                  afterChange();
                  if(selectedId === r.id) showAlert(r);
                } catch (err) {
                  toast('error','Could not resolve', errText(err));
                }
              } },
          ],
        });
      }

      async function mute(r, minutes){
        if(!allowed('operator','Muting an alert requires the operator role.')) return;
        try {
          const res = await Store.mutate(() => API.alerts.mute(r.id, { duration_minutes: minutes, reason: 'Muted from the Alerts screen' }),
            { event:'alerts:changed' });
          const until = res.data && res.data.muted_until;
          toast('info','Muted', until ? `${r.title} — silenced until ${fmtDateTime(ts(until))}.` : (res.message || r.title));
          afterChange();
          if(selectedId === r.id) showAlert(r);
        } catch (err) {
          toast('error','Could not mute', errText(err));
        }
      }

      async function unmute(r){
        if(!allowed('operator','Unmuting an alert requires the operator role.')) return;
        try {
          await Store.mutate(() => API.alerts.update(r.id, { status: 'Open' }), { event:'alerts:changed' });
          toast('info','Unmuted', `${r.title} is open again.`);
          afterChange();
          if(selectedId === r.id) showAlert(r);
        } catch (err) {
          toast('error','Could not unmute', errText(err));
        }
      }

      async function openAssign(r){
        if(!allowed('operator','Assigning an alert requires the operator role.')) return;
        const people = await members.load();
        if(members.error()){
          toast('error','Not permitted','Seeing member names requires the operator role.');
          return;
        }
        if(!people.length){
          toast('warn','No one to assign to','This workspace has no other members yet.');
          return;
        }
        openModal({
          title:'Assign Alert', icon:'users',
          body:`<div class="quote">${esc(r.title)}</div>
            <div class="form-row mt"><label>ASSIGN TO</label>
              <select class="filter-select w-100" id="alAssignee" style="height:34px">${people.map(u =>
                `<option value="${esc(u.id)}" ${u.id === r.assigned_to_user_id ? 'selected' : ''}>${esc(u.full_name)}</option>`).join('')}</select></div>
            <div class="form-row"><label>NOTE (OPTIONAL)</label><input class="input" id="alAssignNote" placeholder="Why them?"></div>`,
          footer:[
            { label:'Cancel' },
            { label:'Assign', cls:'primary', onClick: async (close, modal) => {
                const id = modal.querySelector('#alAssignee').value;
                const note = modal.querySelector('#alAssignNote').value.trim();
                close();
                try {
                  const res = await Store.mutate(() => API.alerts.assign(r.id, note ? { assignee_user_id: id, note } : { assignee_user_id: id }),
                    { event:'alerts:changed' });
                  toast('success','Assigned', `${r.title} → ${members.name(id) || 'the assignee'}. ${res.message || ''}`.trim());
                  afterChange();
                  if(selectedId === r.id) showAlert(r);
                } catch (err) {
                  toast('error','Could not assign', errText(err));
                }
              } },
          ],
        });
      }

      document.getElementById('alAckAll').addEventListener('click', () => {
        if(!allowed('operator','Acknowledging alerts requires the operator role.')) return;
        confirmModal({
          title:'Acknowledge All', icon:'checkCircle', confirmLabel:'Acknowledge All',
          body:`<p style="margin:0">Every Open and Investigating alert in this workspace will be claimed under your name.
            Alerts already acknowledged keep their original timestamp, so time-to-acknowledge stays honest.</p>`,
          onConfirm: async () => {
            try {
              const res = await Store.mutate(() => API.alerts.acknowledgeAll(), { event:'alerts:changed' });
              const n = res.data && res.data.acknowledged;
              toast(n ? 'success' : 'info', n ? 'Alerts acknowledged' : 'Nothing to acknowledge', res.message);
              afterChange();
            } catch (err) {
              toast('error','Could not acknowledge', errText(err));
            }
          },
        });
      });

      // ---- inspector --------------------------------------------------------
      function showAlert(row){
        const insp = document.getElementById('alInspector');
        if(!insp || !row) return;
        selectedId = row.id;
        selectedRow = row;
        document.getElementById('alLayout').classList.remove('collapsed');
        insp.innerHTML = `<div class="insp-head"><div class="grow">
            <div class="insp-title">${esc(row.title)}</div>
            <div class="insp-sub mono">${esc(row.alert_ref || row.id)}</div></div>
            <button class="icon-btn insp-close" id="alClose">${ICONS.x}</button></div>
          <div class="card-loading" style="height:220px;margin:12px"></div>`;
        insp.querySelector('#alClose').addEventListener('click', ()=>document.getElementById('alLayout').classList.add('collapsed'));

        API.alerts.get(row.id)
          .then(a => { if(selectedId === row.id) paintAlert(insp, a); })
          .catch(err => {
            if(selectedId !== row.id) return;
            const holder = insp.querySelector('.card-loading');
            if(holder) holder.replaceWith(screenError(err, ()=>showAlert(row), 'this alert'));
          });
      }

      function paintAlert(insp, a){
        const meta = a.event_metadata || {};
        const metaKeys = Object.keys(meta);
        insp.innerHTML = `
          <div class="insp-head"><div class="grow">
            <div class="flex" style="gap:8px">${badge(a.severity, sevColor(a.severity))}${statusText(a.status, alStatusColor(a.status))}</div>
            <div class="insp-title" style="margin-top:7px">${esc(a.title)}</div>
            <div class="insp-sub mono">${esc(a.alert_ref)}</div></div>
            <button class="icon-btn insp-close" id="alClose">${ICONS.x}</button></div>
          ${inspSection('Details','info', `<div class="quote">${esc(a.description || 'This alert carries no description.')}</div>` + kv([
            ['Source', esc(a.source)],
            ['Entity', a.source_entity_type ? `${esc(a.source_entity_type)} <span class="mono">${esc(String(a.source_entity_id || '').slice(0,12))}</span>` : dash],
            ['First Seen', at(a.raised_at)],
            ['Last Occurrence', a.last_occurred_at ? `${at(a.last_occurred_at)} (${relTime(ts(a.last_occurred_at))})` : dash],
            ['Occurrences', String(a.occurrence_count == null ? 1 : a.occurrence_count)],
            ['Deduplicated On', a.dedupe_key ? `<span class="mono">${esc(a.dedupe_key)}</span>` : '<span class="faint">not deduplicated</span>'],
          ]))}
          ${inspSection('Triage','users', kv([
            ['Assigned To', a.assigned_to_user_id ? members.cell(a.assigned_to_user_id) : '<span class="faint">unassigned</span>'],
            ['Acknowledged', a.acknowledged_at ? `${at(a.acknowledged_at)}` : '<span class="faint">not yet</span>'],
            ['Acknowledged By', a.acknowledged_by_user_id ? members.cell(a.acknowledged_by_user_id) : dash],
            ['Resolved', a.resolved_at ? at(a.resolved_at) : '<span class="faint">still open</span>'],
            ['Resolved By', a.resolved_by_user_id ? members.cell(a.resolved_by_user_id) : dash],
            ['Time to Resolve', dur(a.mttr_seconds)],
          ]))}
          ${metaKeys.length ? inspSection('Alert Payload','terminal',
            kv(metaKeys.map(k => [k, `<span class="mono small">${esc(typeof meta[k] === 'object' ? JSON.stringify(meta[k]) : String(meta[k]))}</span>`]))) : ''}
          ${inspSection('Suggested Runbook','book', `<div class="small dim" style="line-height:1.8">
            1. Open ${esc(a.source)} and confirm the current state of the thing that raised this.<br>
            2. Check Approvals &amp; Audit for changes made around ${esc(a.raised_at ? fmtDateTime(ts(a.raised_at)) : 'the time it was raised')}.<br>
            3. Apply the standard remediation for this alert class.<br>
            4. Resolve with a closing note so the next responder sees what was done.</div>
            <div class="small faint mt">Generic triage guidance — this deployment has no per-rule runbook.</div>`)}
          <div class="insp-section"><div class="grid g2" style="gap:8px">
            <button class="btn sm success" id="alResolveBtn"${gate('operator','Resolving an alert requires the operator role.')}>${ICONS.checkCircle}Resolve</button>
            <button class="btn sm" id="alAckBtn"${gate('operator','Acknowledging an alert requires the operator role.')}>${ICONS.check}Acknowledge</button>
            <button class="btn sm" id="alGoBtn">${ICONS.external}Open Source</button>
            <button class="btn sm" id="alAssignBtn"${gate('operator','Assigning an alert requires the operator role.')}>${ICONS.users}Assign</button>
            <button class="btn sm" id="alMuteBtn"${gate('operator','Muting an alert requires the operator role.')}>${ICONS.clock}Mute 24h</button></div></div>`;

        insp.querySelector('#alClose').addEventListener('click', ()=>document.getElementById('alLayout').classList.add('collapsed'));
        insp.querySelector('#alResolveBtn').addEventListener('click', ()=>resolve(a));
        insp.querySelector('#alAckBtn').addEventListener('click', ()=>acknowledge(a));
        insp.querySelector('#alGoBtn').addEventListener('click', ()=>APP.go(APP.sourceRoute(a.source)));
        insp.querySelector('#alAssignBtn').addEventListener('click', ()=>openAssign(a));
        insp.querySelector('#alMuteBtn').addEventListener('click', ()=>mute(a, 1440));
      }

      // ---- alert rules ------------------------------------------------------
      document.getElementById('alRules').addEventListener('click', openRules);

      function openRules(){
        const handle = openModal({
          title:'Alert Rules', icon:'sliders', wide:true,
          body:`<div id="alRulesBody"><div class="card-loading" style="height:200px"></div></div>`,
          footer:[
            { label:'New Rule', cls:'primary', onClick:(close, modal)=>{
                if(!allowed('admin','Creating an alert rule requires the admin role.')) return;
                editRule(null, ()=>loadRules(modal));
              } },
            { label:'Close' },
          ],
          onOpen(modal){
            loadRules(modal);
            if(!Store.session.can('admin')){
              const btn = modal.querySelector('[data-mbtn="0"]');
              if(btn){ btn.disabled = true; btn.title = 'Creating an alert rule requires the admin role.'; }
            }
          },
        });
        return handle;
      }

      function loadRules(modal){
        const body = modal.querySelector('#alRulesBody');
        if(!body) return;
        body.innerHTML = `<div class="card-loading" style="height:200px"></div>`;
        API.alerts.rules.list({ page_size: 100, sort: 'name' })
          .then(page => {
            if(!modal.querySelector('#alRulesBody')) return;
            if(!(page.items || []).length){
              body.innerHTML = emptyBlock('sliders','No alert rules are defined yet',
                'A rule records the condition a source screen raises its alerts on, and who should hear about them. The screens raise the alerts; the rule is the paper trail reviewers audit against.');
              return;
            }
            body.innerHTML = `<table class="tbl"><thead><tr><th>Rule</th><th>Condition</th><th>Source</th>
                <th>Severity</th><th>Channels</th><th class="right">Throttle</th><th>Status</th><th></th></tr></thead><tbody>
              ${page.items.map((r,i) => `<tr style="cursor:default">
                <td><div class="cell-main">${esc(r.name)}</div>${r.description ? `<div class="cell-sub">${esc(r.description)}</div>` : ''}</td>
                <td class="dim mono small">${esc(JSON.stringify(r.condition || {}))}</td>
                <td class="dim">${esc(r.source)}</td>
                <td>${badge(r.severity, sevColor(r.severity))}</td>
                <td class="dim">${(r.notify_channels || []).length ? esc(r.notify_channels.join(', ')) : '—'}</td>
                <td class="right num">${r.throttle_minutes == null ? '—' : r.throttle_minutes + 'm'}</td>
                <td>${statusText(r.enabled ? 'Active' : 'Disabled', r.enabled ? 'green' : 'gray')}</td>
                <td class="right"><button class="icon-btn" data-rulemenu="${i}">${ICONS.dots}</button></td></tr>`).join('')}
              </tbody></table>
              <div class="small faint mt">${fmtFull(page.total)} rule(s) in this workspace.</div>`;
            body.querySelectorAll('[data-rulemenu]').forEach(btn => {
              btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const rule = page.items[Number(btn.dataset.rulemenu)];
                openMenu(btn, [
                  { label:'Edit Rule', icon:'edit', onClick:()=>editRule(rule, ()=>loadRules(modal)) },
                  { label: rule.enabled ? 'Disable Rule' : 'Enable Rule', icon: rule.enabled ? 'pause' : 'play',
                    onClick:()=>toggleRule(rule, ()=>loadRules(modal)) },
                  { sep:true },
                  { label:'Delete Rule', icon:'trash', danger:true, onClick:()=>deleteRule(rule, ()=>loadRules(modal)) },
                ]);
              });
            });
          })
          .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, ()=>loadRules(modal), 'the alert rules')); });
      }

      function editRule(rule, after){
        if(!allowed('admin', rule ? 'Editing an alert rule requires the admin role.' : 'Creating an alert rule requires the admin role.')) return;
        const r = rule || {};
        openModal({
          title: rule ? 'Edit Alert Rule' : 'New Alert Rule', icon:'sliders',
          body:`<div class="form-row"><label>RULE NAME</label>
              <input class="input" id="arName" value="${esc(r.name || '')}" placeholder="e.g. Connection down"></div>
            <div class="grid g2">
              <div class="form-row"><label>SOURCE SCREEN</label>
                <input class="input" id="arSource" value="${esc(r.source || '')}" placeholder="e.g. Connection Center"></div>
              <div class="form-row"><label>SEVERITY</label>
                <select class="filter-select w-100" id="arSev" style="height:34px">${AL_SEVERITIES.map(s =>
                  `<option ${s === (r.severity || 'Medium') ? 'selected' : ''}>${s}</option>`).join('')}</select></div>
            </div>
            <div class="form-row"><label>DESCRIPTION</label>
              <input class="input" id="arDesc" value="${esc(r.description || '')}" placeholder="What this rule watches for"></div>
            <div class="form-row"><label>CONDITION (JSON)</label>
              <textarea class="input" id="arCond" rows="3" style="font-family:var(--mono,monospace);font-size:12px">${esc(JSON.stringify(r.condition || {}, null, 0))}</textarea>
              <div class="small faint" style="margin-top:4px">Stored exactly as written — the source screen evaluates its own shape.</div></div>
            <div class="grid g2">
              <div class="form-row"><label>NOTIFY CHANNELS</label>
                <input class="input" id="arChan" value="${esc((r.notify_channels || []).join(', '))}" placeholder="${esc(AL_CHANNELS.join(', '))}"></div>
              <div class="form-row"><label>THROTTLE (MINUTES)</label>
                <input class="input" id="arThrottle" value="${r.throttle_minutes == null ? 60 : r.throttle_minutes}"></div>
            </div>
            <label class="flex" style="gap:8px;align-items:center;margin-top:4px">
              <input type="checkbox" id="arEnabled" ${r.enabled === false ? '' : 'checked'}><span class="small">Enabled</span></label>`,
          footer:[
            { label:'Cancel' },
            { label: rule ? 'Save Rule' : 'Create Rule', cls:'primary', onClick: async (close, modal) => {
                const name = modal.querySelector('#arName').value.trim();
                const source = modal.querySelector('#arSource').value.trim();
                if(!name || !source){ toast('error','Cannot save','A rule needs a name and a source screen.'); return; }
                let condition;
                try { condition = JSON.parse(modal.querySelector('#arCond').value.trim() || '{}'); }
                catch (_) { toast('error','Cannot save','The condition is not valid JSON.'); return; }
                const throttle = parseInt(modal.querySelector('#arThrottle').value, 10);
                const body = {
                  name, source, condition,
                  severity: modal.querySelector('#arSev').value,
                  description: modal.querySelector('#arDesc').value.trim() || null,
                  notify_channels: modal.querySelector('#arChan').value.split(',').map(s => s.trim()).filter(Boolean),
                  throttle_minutes: isNaN(throttle) ? 60 : throttle,
                  enabled: modal.querySelector('#arEnabled').checked,
                };
                close();
                try {
                  await Store.mutate(() => rule ? API.alerts.rules.update(rule.id, body) : API.alerts.rules.create(body),
                    { event:'alerts:changed' });
                  toast('success', rule ? 'Rule saved' : 'Rule created', `${name} watches ${source}.`);
                  if(after) after();
                } catch (err) {
                  toast('error', rule ? 'Could not save the rule' : 'Could not create the rule', errText(err));
                }
              } },
          ],
        });
      }

      async function toggleRule(rule, after){
        if(!allowed('admin','Changing an alert rule requires the admin role.')) return;
        try {
          await Store.mutate(() => API.alerts.rules.update(rule.id, { enabled: !rule.enabled }), { event:'alerts:changed' });
          toast('success', rule.enabled ? 'Rule disabled' : 'Rule enabled',
            rule.enabled ? `${rule.name} is retired from ${rule.source}.` : `${rule.name} applies to ${rule.source} again.`);
          if(after) after();
        } catch (err) {
          toast('error','Could not change the rule', errText(err));
        }
      }

      function deleteRule(rule, after){
        if(!allowed('admin','Deleting an alert rule requires the admin role.')) return;
        confirmModal({
          title:'Delete Alert Rule', confirmLabel:'Delete', danger:true,
          msg:`Delete "${rule.name}"? Alerts it already raised are evidence and are kept.`,
          onConfirm: async () => {
            try {
              await Store.mutate(() => API.alerts.rules.remove(rule.id), { event:'alerts:changed' });
              toast('success','Rule deleted', `${rule.name} no longer raises alerts.`);
              if(after) after();
            } catch (err) {
              toast('error','Could not delete the rule', errText(err));
            }
          },
        });
      }
    },
  };

  /* ================= EXPORTS ================= */
  const EX_KPIS = ['Exports (30d)','Scheduled Exports','Total Volume (30d)','Failed Exports'];
  const EX_STATUS = ['Queued','Generating','Ready','Failed','Expired'];
  const EX_STAGES = ['Queued','Generating','Ready'];

  const exStatusColor = (s) => s === 'Ready' ? 'green' : s === 'Failed' ? 'red'
    : s === 'Generating' ? 'amber' : s === 'Expired' ? 'gray' : 'blue';
  const fmtColor = (f) => ({ CSV:'green', JSON:'purple', XLSX:'blue', PDF:'red', Parquet:'cyan' })[f] || 'gray';

  SCREENS['exports'] = {
    title:'Exports',
    render(main){
      const members = memberDirectory();
      let formats = ['CSV'], datasets = [];
      const timers = [];

      main.innerHTML = `
        ${pageHead({title:'Exports', sub:'Generate and download platform data exports for reporting, audit, and BI.',
          actions:`${searchBox('exSearch','Search exports…')}
          <button class="btn" id="exSchedules">${ICONS.calendar}Schedules</button>
          <button class="btn primary" id="exNew"${gate('member','Requesting an export requires the member role.')}>${ICONS.plus}New Export</button>`})}
        <div id="exKpis">${kpiSkeleton(EX_KPIS)}</div>
        <div id="exTableWrap" class="mt"></div>`;

      function loadSummary(){
        const host = document.getElementById('exKpis');
        if(!host) return;
        host.innerHTML = kpiSkeleton(EX_KPIS);
        API.exports.summary()
          .then(s => {
            if(!document.getElementById('exKpis')) return;
            host.innerHTML = kpiRow([
              { label:'Exports (30d)', value:fmtFull(s.exports_30d), icon:'download', color:'purple',
                sub: s.total_rows_30d ? `${fmtFull(s.total_rows_30d)} rows written` : 'No rows written yet' },
              { label:'Scheduled Exports', value:fmtFull(s.scheduled), icon:'calendar', color:'blue',
                sub: s.scheduled ? 'Recurring jobs' : 'None scheduled' },
              { label:'Total Volume (30d)', value:`<span style="font-size:20px">${bytes(s.total_bytes_30d)}</span>`, icon:'hardDrive', color:'green',
                sub: (s.formats || []).length ? s.formats.join(', ') : 'No files produced yet' },
              { label:'Failed Exports', value:fmtFull(s.failed_30d), icon: s.failed_30d ? 'alert' : 'checkCircle',
                color: s.failed_30d ? 'red' : 'cyan',
                sub: `${fmtFull(s.ready)} downloadable now` },
            ]);
            fillScreens(s.source_screens || []);
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSummary, 'the export summary')); });
      }

      function fillScreens(screens){
        const sel = table.filterEl && table.filterEl.querySelector('[data-fi="1"]');
        if(!sel) return;
        const have = new Set(Array.from(sel.options).map(o => o.value || o.textContent));
        screens.forEach(s => {
          if(have.has(s)) return;
          have.add(s);
          const o = document.createElement('option');
          o.textContent = s;
          sel.appendChild(o);
        });
      }

      // ---- the job history --------------------------------------------------
      const table = dataTable({
        columns:[
          { key:'name', label:'Export', render:r => entityCell(r.name, r.source_screen, 'download', 'blue') },
          { key:'export_format', label:'Format', render:r => badge(r.export_format, fmtColor(r.export_format)) },
          { key:'row_count', label:'Rows', align:'right', cls:'num', render:r => num(r.row_count) },
          { key:'size_bytes', label:'Size', align:'right', cls:'num', render:r => `<span class="dim">${bytes(r.size_bytes)}</span>` },
          { key:'requested_by', label:'Requested By', sortable:false, render:r => members.cell(r.requested_by_user_id) },
          { key:'requested_at', label:'Created', render:r => when(r.requested_at) },
          { key:'status', label:'Status', render:r => statusText(r.status, exStatusColor(r.status))
              + (r.is_expired ? ' <span class="badge bg-gray" title="Retention has lapsed">expired</span>' : '') },
        ],
        rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'exports',
        searchPlaceholder:'Search exports…',
        defaultSort:{ key:'requested_at', dir:-1 },
        emptyText:'No exports have been generated in this workspace yet',
        filters:[
          { key:'export_format', label:'Format', param:'export_format', options:['CSV','JSON','XLSX','PDF'], allLabel:'All Formats' },
          { key:'source_screen', label:'Source Screen', param:'source_screen', options:[], allLabel:'All Screens' },
          { key:'status', label:'Status', param:'status', options:EX_STATUS, allLabel:'All Status' },
        ],
        source: (params) => API.exports.list(params),
        exportSource: (params) => API.exports.export(params),
        onSelect: (row) => openJob(row),
        rowActions: r => [
          { label: r.is_downloadable ? 'Download' : 'Download (unavailable)', icon:'download', onClick:()=>download(r) },
          { label:'Re-run Export', icon:'refresh', onClick:()=>rerun(r) },
          { label:'Schedule Weekly', icon:'calendar', onClick:()=>scheduleFrom(r) },
          { label:'Retention', icon:'clock', onClick:()=>showRetention(r) },
          { sep:true },
          { label:'Delete Export', icon:'trash', danger:true, onClick:()=>removeJob(r) },
        ],
      });

      const wrap = document.getElementById('exTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);
      document.getElementById('exSearch').addEventListener('input', e => table.search(e.target.value));

      members.load().then(() => table.refresh());
      loadSummary();
      API.exports.datasets().then(list => { datasets = list || []; }).catch(() => { datasets = []; });
      API.exports.formats().then(list => { if((list || []).length) formats = list; }).catch(() => {});

      function refreshAll(){ table.refresh(); loadSummary(); }

      // ---- row actions ------------------------------------------------------
      async function download(r){
        if(!r.is_downloadable){
          toast('warn','Not downloadable',
            r.status === 'Failed' ? (r.error || 'This export failed and produced no file.')
              : r.is_expired ? 'This file has passed its retention window and has been dropped.'
              : 'The file is still being generated.');
          return;
        }
        try {
          const name = await API.exports.download(r.id);
          toast('success','Download started', name);
          table.refresh();
        } catch (err) {
          toast('error','Could not download', errText(err));
        }
      }

      async function rerun(r){
        if(!allowed('member','Re-running an export requires the member role.')) return;
        try {
          const job = await Store.mutate(() => API.exports.rerun(r.id), { event:'exports:changed' });
          toast('info','Export queued', `${job.name} — ${job.export_ref}`);
          refreshAll();
          watchJob(job);
        } catch (err) {
          toast('error','Could not re-run', errText(err));
        }
      }

      function removeJob(r){
        if(!allowed('operator','Deleting an export requires the operator role.')) return;
        confirmModal({
          title:'Delete Export', confirmLabel:'Delete', danger:true,
          msg:`Delete "${r.name}"? The generated file is removed with it and cannot be downloaded afterwards.`,
          onConfirm: async () => {
            try {
              await Store.mutate(() => API.exports.remove(r.id), { event:'exports:changed' });
              toast('success','Export deleted', r.name);
              refreshAll();
            } catch (err) {
              toast('error','Could not delete', errText(err));
            }
          },
        });
      }

      function showRetention(r){
        openModal({
          title:'Retention — ' + r.name, icon:'clock',
          body:`<div class="card-loading" style="height:120px"></div>`,
          footer:[{ label:'Close' }],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.exports.retention(r.id)
              .then(res => {
                const d = res.data || {};
                body.innerHTML = kv([
                  ['Status', statusText(d.status || r.status, exStatusColor(d.status || r.status))],
                  ['Retention Window', d.retention_days == null ? dash : `${d.retention_days} days`],
                  ['Expires', d.expires_at ? `${at(d.expires_at)} (${until(d.expires_at)})` : dash],
                  ['Row Cap Per Export', d.max_rows == null ? dash : fmtFull(d.max_rows)],
                  ['Downloads', num(r.download_count)],
                ]) + `<div class="quote mt">${ICONS.info} ${esc(res.message || 'Retention is a deployment setting, not a per-job one.')}</div>`;
              })
              .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, null, 'the retention policy')); });
          },
        });
      }

      function openJob(r){
        openModal({
          title:'Export — ' + r.name, icon:'download', wide:true,
          body:`<div class="card-loading" style="height:160px"></div>`,
          footer:[{ label:'Close' }],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.exports.get(r.id)
              .then(j => {
                const fk = Object.keys(j.filters || {});
                body.innerHTML = kv([
                  ['Reference', `<span class="mono">${esc(j.export_ref)}</span>`],
                  ['Dataset', esc(j.source_screen)],
                  ['Format', badge(j.export_format, fmtColor(j.export_format))],
                  ['Status', statusText(j.status, exStatusColor(j.status))],
                  ['Rows', num(j.row_count)],
                  ['Size', bytes(j.size_bytes)],
                  ['Requested', `${at(j.requested_at)}`],
                  ['Requested By', members.cell(j.requested_by_user_id)],
                  ['Completed', at(j.completed_at)],
                  ['Expires', j.expires_at ? `${at(j.expires_at)} (${until(j.expires_at)})` : dash],
                  ['Downloads', num(j.download_count)],
                  ...(j.error ? [['Error', `<span class="st-red">${esc(j.error)}</span>`]] : []),
                ]) + (fk.length
                    ? `<div class="small faint mt">Filters replayed for this extract</div>`
                      + kv(fk.map(k => [k, `<span class="mono small">${esc(String(j.filters[k]))}</span>`]))
                    : `<div class="small faint mt">No filters were applied — this is the whole dataset.</div>`)
                  + `<div class="flex mt" style="gap:8px">
                      <button class="btn sm primary" id="exJobDl"${j.is_downloadable ? '' : ' disabled title="No file is available for this job"'}>${ICONS.download}Download</button>
                      <button class="btn sm" id="exJobRerun"${gate('member','Re-running an export requires the member role.')}>${ICONS.refresh}Re-run</button></div>`;
                const dl = body.querySelector('#exJobDl');
                if(dl) dl.addEventListener('click', ()=>download(j));
                const rr = body.querySelector('#exJobRerun');
                if(rr) rr.addEventListener('click', ()=>rerun(j));
              })
              .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, null, 'this export job')); });
          },
        });
      }

      // ---- new export -------------------------------------------------------
      document.getElementById('exNew').addEventListener('click', () => {
        if(!allowed('member','Requesting an export requires the member role.')) return;
        if(!datasets.length){
          toast('error','No datasets available','The control plane did not offer any exportable dataset.');
          return;
        }
        openModal({
          title:'New Export', icon:'download',
          body:`<div class="form-row"><label>DATASET</label>
              <select class="filter-select w-100" id="nexDs" style="height:34px">${datasets.map(d =>
                `<option value="${esc(d.source_screen)}">${esc(d.source_screen)} — ${d.columns.length} columns</option>`).join('')}</select>
              <div class="small faint" id="nexCols" style="margin-top:4px"></div></div>
            <div class="grid g2">
              <div class="form-row"><label>FORMAT</label>
                <select class="filter-select w-100" id="nexFmt" style="height:34px">${formats.map(f => `<option>${esc(f)}</option>`).join('')}</select></div>
              <div class="form-row"><label>TIME RANGE</label>
                <select class="filter-select w-100" id="nexDays" style="height:34px">
                  <option value="1">Last 24 hours</option><option value="7">Last 7 days</option>
                  <option value="30" selected>Last 30 days</option><option value="90">Last 90 days</option>
                  <option value="">All time</option></select></div>
            </div>
            <div class="form-row"><label>NAME</label><input class="input" id="nexName" placeholder="Defaults to the dataset and today's date"></div>`,
          footer:[
            { label:'Cancel' },
            { label:'Generate Export', cls:'primary', onClick: async (close, modal) => {
                const ds = modal.querySelector('#nexDs').value;
                const fmt = modal.querySelector('#nexFmt').value;
                const days = modal.querySelector('#nexDays').value;
                const name = modal.querySelector('#nexName').value.trim() || `${ds} — ${fmtDate(Date.now())}`;
                const body = { name, source_screen: ds, export_format: fmt, filters: days ? { days: Number(days) } : {} };
                close();
                try {
                  const job = await Store.mutate(() => API.exports.create(body), { event:'exports:changed' });
                  toast('info','Export queued', `${job.name} — ${job.export_ref}`);
                  refreshAll();
                  watchJob(job);
                } catch (err) {
                  toast('error','Could not queue the export', errText(err));
                }
              } },
          ],
          onOpen(modal){
            const sel = modal.querySelector('#nexDs');
            const note = modal.querySelector('#nexCols');
            const paintCols = () => {
              const d = datasets.find(x => x.source_screen === sel.value);
              note.textContent = d ? `Columns: ${d.columns.join(', ')}` : '';
            };
            sel.addEventListener('change', paintCols);
            paintCols();
          },
        });
      });

      /**
       * Watch a queued job through to its file.
       *
       * The bar is the server's own three-stage status, not a timer: Queued,
       * Generating, then Ready or Failed. Polling stops the moment the job is
       * terminal or the modal is closed.
       */
      function watchJob(job){
        let timer = null, closed = false;
        const handle = openModal({
          title:'Generating Export', icon:'download',
          body:`<div id="exJobBody"><div class="card-loading" style="height:120px"></div></div>`,
          footer:[{ label:'Run in Background', onClick:(close)=>{ close(); stop(); } }],
          onOpen(modal){ tick(modal); },
        });
        // The poll outlives the render that started it; navigating away ends both
        // the polling and the window that was reporting it.
        timers.push(() => { stop(); handle.close(); });
        const overlay = handle.el.closest('.modal-overlay');
        const watcher = new MutationObserver(()=>{ if(!document.body.contains(handle.el)) stop(); });
        if(overlay && overlay.parentNode) watcher.observe(overlay.parentNode, { childList:true });

        function stop(){ closed = true; if(timer) clearTimeout(timer); timer = null; watcher.disconnect(); }

        function tick(modal){
          if(closed) return;
          API.exports.status(job.id)
            .then(j => {
              if(closed) return;
              const body = modal.querySelector('#exJobBody');
              if(!body){ stop(); return; }
              body.innerHTML = paint(j);
              const dl = body.querySelector('#exWatchDl');
              if(dl) dl.addEventListener('click', ()=>download(j));
              if(j.status === 'Ready' || j.status === 'Failed' || j.status === 'Expired'){
                stop();
                refreshAll();
                toast(j.status === 'Ready' ? 'success' : 'error',
                  j.status === 'Ready' ? 'Export ready' : 'Export failed',
                  j.status === 'Ready'
                    ? `${j.name} — ${fmtFull(j.row_count)} rows, ${bytes(j.size_bytes)}.`
                    : (j.error || 'The generator reported a failure.'));
                return;
              }
              timer = setTimeout(()=>tick(modal), 1200);
            })
            .catch(err => {
              if(closed) return;
              stop();
              const body = modal.querySelector('#exJobBody');
              if(body){ body.innerHTML = ''; body.appendChild(screenError(err, null, 'the export status')); }
            });
        }

        function paint(j){
          const idx = EX_STAGES.indexOf(j.status);
          return `<div class="flex" style="gap:6px;margin-bottom:10px">${EX_STAGES.map((s,i) =>
              `<span class="badge bg-${j.status === 'Failed' ? (i === 0 ? 'gray' : 'red')
                : idx < 0 ? 'gray' : i < idx ? 'green' : i === idx ? 'amber' : 'gray'}">${s}</span>`).join('')}</div>
            ${kv([
              ['Reference', `<span class="mono">${esc(j.export_ref)}</span>`],
              ['Dataset', esc(j.source_screen)],
              ['Format', badge(j.export_format, fmtColor(j.export_format))],
              ['Status', statusText(j.status, exStatusColor(j.status))],
              ['Rows', num(j.row_count)],
              ['Size', bytes(j.size_bytes)],
              ['Completed', at(j.completed_at)],
              ...(j.error ? [['Error', `<span class="st-red">${esc(j.error)}</span>`]] : []),
            ])}
            ${j.is_downloadable ? `<button class="btn sm primary mt" id="exWatchDl">${ICONS.download}Download</button>` : ''}`;
        }
      }

      // ---- schedules --------------------------------------------------------
      document.getElementById('exSchedules').addEventListener('click', openSchedules);

      function openSchedules(){
        openModal({
          title:'Export Schedules', icon:'calendar', wide:true,
          body:`<div id="exSchedBody"><div class="card-loading" style="height:200px"></div></div>`,
          footer:[
            { label:'New Schedule', cls:'primary', onClick:(close, modal)=>{
                if(!allowed('operator','Scheduling an export requires the operator role.')) return;
                editSchedule(null, ()=>loadSchedules(modal));
              } },
            { label:'Close' },
          ],
          onOpen(modal){
            loadSchedules(modal);
            if(!Store.session.can('operator')){
              const btn = modal.querySelector('[data-mbtn="0"]');
              if(btn){ btn.disabled = true; btn.title = 'Scheduling an export requires the operator role.'; }
            }
          },
        });
      }

      function loadSchedules(modal){
        const body = modal.querySelector('#exSchedBody');
        if(!body) return;
        body.innerHTML = `<div class="card-loading" style="height:200px"></div>`;
        API.exports.schedules.list({ page_size: 100, sort: 'name' })
          .then(page => {
            if(!modal.querySelector('#exSchedBody')) return;
            if(!(page.items || []).length){
              body.innerHTML = emptyBlock('calendar','No export runs on a schedule yet',
                'A schedule re-runs a dataset on a cron cadence and files the result here.');
              return;
            }
            body.innerHTML = `<table class="tbl"><thead><tr><th>Schedule</th><th>Dataset</th><th>Format</th><th>Cron</th>
                <th>Next Run</th><th>Last Run</th><th>Recipients</th><th>Status</th><th></th></tr></thead><tbody>
              ${page.items.map((s,i) => `<tr style="cursor:default">
                <td class="cell-main">${esc(s.name)}</td>
                <td class="dim">${esc(s.source_screen)}</td>
                <td>${badge(s.export_format, fmtColor(s.export_format))}</td>
                <td class="mono small">${esc(s.cron)}</td>
                <td class="dim nowrap">${s.next_run_at ? esc(fmtDateTime(ts(s.next_run_at))) : '—'}</td>
                <td class="dim nowrap">${s.last_run_at ? esc(relTime(ts(s.last_run_at))) : '—'}</td>
                <td class="dim">${(s.recipients || []).length ? esc(s.recipients.join(', ')) : '—'}</td>
                <td>${statusText(s.enabled ? 'Active' : 'Paused', s.enabled ? 'green' : 'gray')}</td>
                <td class="right"><button class="icon-btn" data-schedmenu="${i}">${ICONS.dots}</button></td></tr>`).join('')}
              </tbody></table>
              <div class="small faint mt">${fmtFull(page.total)} schedule(s).</div>`;
            body.querySelectorAll('[data-schedmenu]').forEach(btn => {
              btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const s = page.items[Number(btn.dataset.schedmenu)];
                openMenu(btn, [
                  { label:'Run Now', icon:'play', onClick:()=>runSchedule(s, ()=>loadSchedules(modal)) },
                  { label:'Edit Schedule', icon:'edit', onClick:()=>editSchedule(s, ()=>loadSchedules(modal)) },
                  { label: s.enabled ? 'Pause Schedule' : 'Resume Schedule', icon: s.enabled ? 'pause' : 'play',
                    onClick:()=>toggleSchedule(s, ()=>loadSchedules(modal)) },
                  { sep:true },
                  { label:'Delete Schedule', icon:'trash', danger:true, onClick:()=>deleteSchedule(s, ()=>loadSchedules(modal)) },
                ]);
              });
            });
          })
          .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, ()=>loadSchedules(modal), 'the export schedules')); });
      }

      function editSchedule(sched, after){
        // "Schedule Weekly" seeds a schedule that has no id yet — only an id means an edit.
        const isEdit = !!(sched && sched.id);
        if(!allowed('operator', isEdit ? 'Editing a schedule requires the operator role.' : 'Scheduling an export requires the operator role.')) return;
        const s = sched || {};
        const dsOptions = datasets.length
          ? datasets.map(d => `<option ${d.source_screen === s.source_screen ? 'selected' : ''}>${esc(d.source_screen)}</option>`).join('')
          : `<option>${esc(s.source_screen || '')}</option>`;
        openModal({
          title: isEdit ? 'Edit Schedule' : 'New Export Schedule', icon:'calendar',
          body:`<div class="form-row"><label>NAME</label>
              <input class="input" id="esName" value="${esc(s.name || '')}" placeholder="e.g. Weekly audit trail"></div>
            <div class="grid g2">
              <div class="form-row"><label>DATASET</label>
                <select class="filter-select w-100" id="esDs" style="height:34px">${dsOptions}</select></div>
              <div class="form-row"><label>FORMAT</label>
                <select class="filter-select w-100" id="esFmt" style="height:34px">${formats.map(f =>
                  `<option ${f === s.export_format ? 'selected' : ''}>${esc(f)}</option>`).join('')}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>CRON (UTC)</label>
                <input class="input" id="esCron" value="${esc(s.cron || '0 6 * * 1')}" placeholder="0 6 * * 1"></div>
              <div class="form-row"><label>TIME RANGE (DAYS)</label>
                <input class="input" id="esDays" value="${esc(String((s.filters && s.filters.days) || 7))}"></div>
            </div>
            <div class="form-row"><label>RECIPIENTS</label>
              <input class="input" id="esRcpt" value="${esc((s.recipients || []).join(', '))}" placeholder="ops@example.com, audit@example.com"></div>
            <label class="flex" style="gap:8px;align-items:center;margin-top:4px">
              <input type="checkbox" id="esEnabled" ${s.enabled === false ? '' : 'checked'}><span class="small">Enabled</span></label>
            <div class="small faint" style="margin-top:6px">The cron expression is evaluated in UTC; the first firing is computed when it is saved.</div>`,
          footer:[
            { label:'Cancel' },
            { label: isEdit ? 'Save Schedule' : 'Create Schedule', cls:'primary', onClick: async (close, modal) => {
                const name = modal.querySelector('#esName').value.trim();
                const cron = modal.querySelector('#esCron').value.trim();
                if(!name || !cron){ toast('error','Cannot save','A schedule needs a name and a cron expression.'); return; }
                const days = parseInt(modal.querySelector('#esDays').value, 10);
                const body = {
                  name, cron,
                  source_screen: modal.querySelector('#esDs').value,
                  export_format: modal.querySelector('#esFmt').value,
                  filters: isNaN(days) ? {} : { days },
                  recipients: modal.querySelector('#esRcpt').value.split(',').map(x => x.trim()).filter(Boolean),
                  enabled: modal.querySelector('#esEnabled').checked,
                };
                close();
                try {
                  const saved = await Store.mutate(() => isEdit
                    ? API.exports.schedules.update(sched.id, body)
                    : API.exports.schedules.create(body), { event:'exports:changed' });
                  toast('success', isEdit ? 'Schedule saved' : 'Schedule created',
                    saved.next_run_at ? `Next run ${fmtDateTime(ts(saved.next_run_at))}.` : `${name} is paused until enabled.`);
                  loadSummary();
                  if(after) after();
                } catch (err) {
                  toast('error', isEdit ? 'Could not save the schedule' : 'Could not create the schedule', errText(err));
                }
              } },
          ],
        });
      }

      async function toggleSchedule(s, after){
        if(!allowed('operator','Changing a schedule requires the operator role.')) return;
        try {
          await Store.mutate(() => API.exports.schedules.update(s.id, { enabled: !s.enabled }), { event:'exports:changed' });
          toast('success', s.enabled ? 'Schedule paused' : 'Schedule resumed', s.name);
          loadSummary();
          if(after) after();
        } catch (err) {
          toast('error','Could not change the schedule', errText(err));
        }
      }

      async function runSchedule(s, after){
        if(!allowed('operator','Running a schedule requires the operator role.')) return;
        try {
          const job = await Store.mutate(() => API.exports.runSchedule(s.id), { event:'exports:changed' });
          toast('info','Schedule fired', `${job.name} — ${job.export_ref}`);
          refreshAll();
          if(after) after();
          watchJob(job);
        } catch (err) {
          toast('error','Could not run the schedule', errText(err));
        }
      }

      function deleteSchedule(s, after){
        if(!allowed('operator','Deleting a schedule requires the operator role.')) return;
        confirmModal({
          title:'Delete Schedule', confirmLabel:'Delete', danger:true,
          msg:`Stop "${s.name}"? Files it already produced are kept.`,
          onConfirm: async () => {
            try {
              await Store.mutate(() => API.exports.schedules.remove(s.id), { event:'exports:changed' });
              toast('success','Schedule deleted', s.name);
              loadSummary();
              if(after) after();
            } catch (err) {
              toast('error','Could not delete the schedule', errText(err));
            }
          },
        });
      }

      /** The "Schedule Weekly" row action: a schedule seeded from an existing job. */
      function scheduleFrom(r){
        if(!allowed('operator','Scheduling an export requires the operator role.')) return;
        editSchedule({
          name: `${r.name} — weekly`,
          source_screen: r.source_screen,
          export_format: r.export_format,
          cron: '0 6 * * 1',
          filters: r.filters || {},
          recipients: [],
          enabled: true,
        }, null);
      }

      this.cleanup = () => { timers.forEach(fn => { try { fn(); } catch (_) {} }); };
    },
  };

  /* ================= LICENSING & ENTITLEMENTS ================= */
  const LI_KPIS = ['License Plans','Active Tenant Licenses','Seats Assigned','Expiring in 30 Days','Suspended / Revoked','Overage Alerts'];
  const PLAN_TIERS = ['Starter','Professional','Enterprise','Custom','Pro','Standard','Trial','Free'];
  const PLAN_STATUS = ['Active','Draft','Retired'];
  const LICENSE_STATUS = ['Active','Trial','Expiring Soon','Suspended','Revoked','Expired'];
  const INVOICE_STATUS = ['Draft','Issued','Paid','Overdue','Void'];
  const SEAT_ROLES = ['owner','admin','operator','approver','member','viewer'];
  const BILLING_PERIODS = ['Monthly','Annual'];

  const tierColor = { Enterprise:'purple', Professional:'blue', Pro:'blue', Standard:'green',
                      Starter:'cyan', Custom:'orange', Trial:'amber', Free:'gray' };
  const licColor = (s) => s === 'Active' ? 'green' : s === 'Trial' ? 'amber' : s === 'Expiring Soon' ? 'amber'
    : s === 'Suspended' ? 'red' : s === 'Revoked' ? 'red' : 'gray';
  const planColor = (s) => s === 'Active' ? 'green' : s === 'Draft' ? 'amber' : 'gray';
  const invColor = (s) => s === 'Paid' ? 'green' : s === 'Overdue' ? 'red' : s === 'Issued' ? 'amber' : 'gray';

  const LI_TABS = ['Plans & Tiers','Tenants & Licenses','Seats & Assignments','Usage & Limits','Billing & Renewals','Audit Log'];
  const LI_DATASETS = ['plans','tenants','seats','usage','invoices'];

  SCREENS['licensing'] = {
    title:'Licensing & Entitlements',
    render(main){
      const members = memberDirectory();
      let activeTab = 0, activeTable = null, license = null, licenseLoaded = false;

      main.innerHTML = `
        ${pageHead({title:'Licensing & Entitlements', sub:'Define plans, manage seats and usage limits, and enforce entitlements across tenants.',
          actions:`${searchBox('liSearch','Search plans, tenants, licenses…')}
          <button class="btn" id="liExport">${ICONS.download}Export</button>
          <button class="btn primary" id="liNew"${gate('owner','Creating a license plan requires the owner role.')}>${ICONS.plus}Create License Plan</button>`})}
        <div id="liKpis">${kpiSkeleton(LI_KPIS, 180)}</div>
        <div id="liTabs"></div>
        <div id="liBody"></div>`;

      const body = document.getElementById('liBody');

      function loadSummary(){
        const host = document.getElementById('liKpis');
        if(!host) return;
        host.innerHTML = kpiSkeleton(LI_KPIS, 180);
        API.licensing.summary()
          .then(s => {
            if(!document.getElementById('liKpis')) return;
            host.innerHTML = kpiRow([
              { label:'License Plans', value:fmtFull(s.plans), icon:'package', color:'purple',
                sub:`${fmtFull(s.active_plans)} on sale` },
              { label:'Active Tenant Licenses', value:fmtFull(s.active_tenant_licenses), icon:'globe', color:'green',
                sub: s.trial_licenses ? `${fmtFull(s.trial_licenses)} on trial` : 'No trials running' },
              { label:'Seats Assigned', value:fmtFull(s.seats_assigned), icon:'users', color:'blue',
                sub:`${fmtFull(s.seats_purchased)} purchased · ${pct(s.seat_utilization_pct)} used` },
              { label:'Expiring in 30 Days', value:fmtFull(s.expiring_in_30_days), icon:'clock', color:'amber',
                sub: s.expiring_in_30_days ? 'Renewal work outstanding' : 'Nothing inside the horizon' },
              { label:'Suspended / Revoked', value:fmtFull(s.suspended_or_revoked), icon:'xCircle', color:'red',
                sub: s.suspended_or_revoked ? 'Entitlements are refusing' : 'All licences in service' },
              { label:'Overage Alerts', value:fmtFull(s.overage_alerts), icon:'alert', color:'red',
                sub:`${fmtFull(s.open_invoices)} open invoice(s) · ${money(s.outstanding_usd)} outstanding` },
            ], 180);
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSummary, 'the licensing summary')); });
      }
      loadSummary();
      members.load();

      /** Which licence the seat, usage and billing tabs are about. */
      function currentLicense(){
        if(licenseLoaded) return Promise.resolve(license);
        return API.licensing.tenants.list({ page_size: 1, sort: '-created_at' })
          .then(page => { license = (page.items || [])[0] || null; licenseLoaded = true; return license; });
      }

      const tabsEl = tabBar(document.getElementById('liTabs'), LI_TABS.map(l => ({ label:l })), switchTab);
      function goTab(i){
        tabsEl.querySelectorAll('.tab').forEach((t, ti) => t.classList.toggle('active', ti === i));
        switchTab(i);
      }

      function switchTab(i){
        activeTab = i;
        activeTable = null;
        body.innerHTML = '';
        if(i === 0) tabPlans();
        else if(i === 1) tabTenants();
        else if(i === 2) tabSeats();
        else if(i === 3) tabUsage();
        else if(i === 4) tabBilling();
        else tabAudit();
      }

      document.getElementById('liSearch').addEventListener('input', e => {
        if(activeTable) activeTable.search(e.target.value);
      });

      document.getElementById('liExport').addEventListener('click', async () => {
        try {
          if(activeTab === 5){
            await API.audit.export({ source_screen:'Licensing & Entitlements', page_size: 500 });
            toast('success','Export complete','Licensing audit trail exported to CSV.');
            return;
          }
          const params = { dataset: LI_DATASETS[activeTab] };
          if(activeTable){
            const p = activeTable.params();
            if(p.q) params.q = p.q;
            if(p.sort) params.sort = p.sort;
            ['status','tier','billing_period','plan_id','license_id','role','state'].forEach(k => {
              if(p[k]) params[k] = p[k];
            });
          }
          if((activeTab === 2 || activeTab === 3) && license) params.license_id = license.id;
          await API.licensing.export(params);
          toast('success','Export complete', `Licensing ${LI_DATASETS[activeTab]} exported to CSV.`);
        } catch (err) {
          toast('error','Export failed', errText(err));
        }
      });

      // ---- tab 0: plans & tiers ---------------------------------------------
      function tabPlans(){
        body.innerHTML = `<div class="two-col">
          <div id="liPlansTbl"></div>
          <div style="display:flex;flex-direction:column;gap:14px">
            <div class="card" id="liEntCard"><div class="card-loading" style="height:150px"></div></div>
            <div class="card" id="liUtilCard"><div class="card-loading" style="height:150px"></div></div>
          </div></div>`;

        const t = dataTable({
          columns:[
            { key:'name', label:'Plan', render:r => entityCell(r.name, r.code, 'package', tierColor[r.tier] || 'gray') },
            { key:'tier', label:'Tier', render:r => badge(r.tier, tierColor[r.tier] || 'gray') },
            { key:'billing_period', label:'Type', render:r => `<span class="dim">${esc(r.billing_period || '—')}</span>` },
            { key:'price_per_seat_usd', label:'Per Seat', align:'right', cls:'num', render:r => money(r.price_per_seat_usd) },
            { key:'seats', label:'Seats Used', align:'right', cls:'num', sortable:false,
              render:r => `${fmtFull(r.seats_assigned)} / ${fmtFull(r.seats_purchased)}` },
            { key:'status', label:'Status', render:r => statusText(r.status, planColor(r.status)) },
            { key:'utilization', label:'Utilization', sortable:false, render:r => r.seat_utilization_pct == null ? dash
                : barPct(r.seat_utilization_pct, r.seat_utilization_pct >= 85 ? 'amber' : r.seat_utilization_pct === 0 ? 'gray' : 'green',
                    r.seat_utilization_pct.toFixed(1) + '%') },
            { key:'effective_to', label:'Renewal', render:r => r.effective_to ? `<span class="dim nowrap">${day(r.effective_to)}</span>` : '<span class="faint">open-ended</span>' },
          ],
          rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'plans',
          searchPlaceholder:'Search plans…',
          emptyText:'No license plans have been published yet',
          filters:[
            { key:'tier', label:'Tier', param:'tier', options:PLAN_TIERS, allLabel:'All Tiers' },
            { key:'status', label:'Status', param:'status', options:PLAN_STATUS, allLabel:'All Status' },
            { key:'billing_period', label:'Billing', param:'billing_period', options:BILLING_PERIODS, allLabel:'All Periods' },
          ],
          source: (params) => API.licensing.plans.list(params),
          onLoad: (rows) => paintPlanCards(rows),
          rowActions: r => [
            { label:'Edit Plan', icon:'edit', onClick:()=>editPlan(r, ()=>t.refresh()) },
            { label:'Manage Seats', icon:'users', onClick:()=>goTab(2) },
            { label:'View Billing', icon:'creditCard', onClick:()=>goTab(4) },
            { sep:true },
            r.status !== 'Retired'
              ? { label:'Retire Plan', icon:'xCircle', danger:true, onClick:()=>setPlanStatus(r, 'Retired', ()=>t.refresh()) }
              : { label:'Reactivate Plan', icon:'checkCircle', onClick:()=>setPlanStatus(r, 'Active', ()=>t.refresh()) },
            { label:'Delete Plan', icon:'trash', danger:true, onClick:()=>deletePlan(r, ()=>t.refresh()) },
          ],
        });
        activeTable = t;
        const holder = document.getElementById('liPlansTbl');
        holder.appendChild(t.filterEl);
        holder.appendChild(t.el);
      }

      /** The entitlement matrix and the utilisation bars, both built from the plans on screen. */
      function paintPlanCards(rows){
        const ent = document.getElementById('liEntCard');
        const util = document.getElementById('liUtilCard');
        if(!ent || !util) return;
        const plans = (rows || []).slice(0, 3);
        const features = [];
        plans.forEach(p => (p.features || []).forEach(f => { if(!features.includes(f)) features.push(f); }));
        ent.innerHTML = `<div class="card-head"><div class="card-title">Feature Entitlements</div>
            <span class="small faint">${plans.length ? esc(plans.map(p => p.tier || p.name).join(' · ')) : ''}</span></div>
          ${!plans.length ? emptyBlock('package','No plan to compare','Publish a plan and its features appear here.')
            : !features.length ? emptyBlock('package','These plans list no features','A plan’s feature bullets drive this matrix.')
            : features.map(f => `<div class="kv"><span class="k" style="color:var(--text);font-size:11.5px">${esc(f)}</span>
              <span class="v flex" style="gap:12px">${plans.map(p => (p.features || []).includes(f)
                ? `<span style="color:#15803D;width:13px;display:inline-flex" title="${esc(p.name)}">${ICONS.check}</span>`
                : `<span style="color:#94A3B8;width:13px;display:inline-flex" title="${esc(p.name)}">${ICONS.x}</span>`).join('')}</span></div>`).join('')}`;

        const used = (rows || []).filter(p => p.seat_utilization_pct > 0);
        util.innerHTML = `<div class="card-head"><div class="card-title">Seat Utilization by Plan</div></div>
          ${used.length
            ? hbars(used.map(p => ({ label:p.name, value:p.seat_utilization_pct,
                color: p.seat_utilization_pct >= 85 ? 'amber' : 'green',
                display: p.seat_utilization_pct.toFixed(0) + '%' })), { labelW:150 })
            : emptyBlock('users','No plan has a seat assigned yet','Issue a licence and assign seats to see utilisation.')}`;
      }

      function editPlan(plan, after){
        if(!allowed('owner', plan ? 'Editing a plan requires the owner role.' : 'Creating a plan requires the owner role.')) return;
        const p = plan || {};
        openModal({
          title: plan ? 'Edit License Plan' : 'Create License Plan', icon:'package',
          body:`<div class="grid g2">
              <div class="form-row"><label>PLAN NAME</label>
                <input class="input" id="lpName" value="${esc(p.name || '')}" placeholder="e.g. Enterprise Plus"></div>
              <div class="form-row"><label>PLAN CODE</label>
                <input class="input" id="lpCode" value="${esc(p.code || '')}" placeholder="e.g. enterprise-plus"></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>TIER</label>
                <select class="filter-select w-100" id="lpTier" style="height:34px">${PLAN_TIERS.map(x =>
                  `<option ${x === (p.tier || 'Professional') ? 'selected' : ''}>${x}</option>`).join('')}</select></div>
              <div class="form-row"><label>BILLING PERIOD</label>
                <select class="filter-select w-100" id="lpPeriod" style="height:34px">${BILLING_PERIODS.map(x =>
                  `<option ${x === (p.billing_period || 'Monthly') ? 'selected' : ''}>${x}</option>`).join('')}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>PRICE PER SEAT (USD)</label>
                <input class="input" id="lpPrice" value="${esc(String(p.price_per_seat_usd == null ? '40' : p.price_per_seat_usd))}"></div>
              <div class="form-row"><label>INCLUDED SEATS</label>
                <input class="input" id="lpSeats" value="${esc(String(p.included_seats == null ? 100 : p.included_seats))}"></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>INCLUDED TOKENS</label>
                <input class="input" id="lpTokens" value="${esc(String(p.included_tokens == null ? 1000000 : p.included_tokens))}"></div>
              <div class="form-row"><label>INCLUDED RUNS</label>
                <input class="input" id="lpRuns" value="${esc(String(p.included_runs == null ? 10000 : p.included_runs))}"></div>
            </div>
            <div class="form-row"><label>FEATURES (COMMA SEPARATED)</label>
              <input class="input" id="lpFeat" value="${esc((p.features || []).join(', '))}" placeholder="MCP Governance, Policy Center, SSO"></div>
            <div class="grid g2">
              <div class="form-row"><label>STATUS</label>
                <select class="filter-select w-100" id="lpStatus" style="height:34px">${PLAN_STATUS.map(x =>
                  `<option ${x === (p.status || 'Draft') ? 'selected' : ''}>${x}</option>`).join('')}</select></div>
              <div class="form-row"><label>OVERAGE PER 1K TOKENS (USD)</label>
                <input class="input" id="lpOverage" value="${esc(String(p.overage_rate_per_1k_tokens == null ? '0.5' : p.overage_rate_per_1k_tokens))}"></div>
            </div>`,
          footer:[
            { label:'Cancel' },
            { label: plan ? 'Save Plan' : 'Create Plan', cls:'primary', onClick: async (close, modal) => {
                const name = modal.querySelector('#lpName').value.trim();
                const code = modal.querySelector('#lpCode').value.trim();
                if(!name || !code){ toast('error','Cannot save','A plan needs a name and a code.'); return; }
                const intOf = (id, fallback) => { const v = parseInt(modal.querySelector(id).value, 10); return isNaN(v) ? fallback : v; };
                const payload = {
                  name, code,
                  tier: modal.querySelector('#lpTier').value,
                  billing_period: modal.querySelector('#lpPeriod').value,
                  price_per_seat_usd: modal.querySelector('#lpPrice').value.trim() || '0',
                  included_seats: intOf('#lpSeats', 0),
                  included_tokens: intOf('#lpTokens', 0),
                  included_runs: intOf('#lpRuns', 0),
                  overage_rate_per_1k_tokens: modal.querySelector('#lpOverage').value.trim() || '0',
                  features: modal.querySelector('#lpFeat').value.split(',').map(x => x.trim()).filter(Boolean),
                  status: modal.querySelector('#lpStatus').value,
                };
                close();
                try {
                  await Store.mutate(() => plan ? API.licensing.plans.update(plan.id, payload) : API.licensing.plans.create(payload),
                    { event:'licensing:changed' });
                  toast('success', plan ? 'Plan saved' : 'Plan created',
                    plan ? `${name} updated.` : `${name} is available for tenant assignment.`);
                  loadSummary();
                  if(after) after();
                } catch (err) {
                  toast('error', plan ? 'Could not save the plan' : 'Could not create the plan', errText(err));
                }
              } },
          ],
        });
      }

      async function setPlanStatus(plan, status, after){
        if(!allowed('owner','Changing a plan requires the owner role.')) return;
        try {
          await Store.mutate(() => API.licensing.plans.update(plan.id, { status }), { event:'licensing:changed' });
          toast(status === 'Retired' ? 'warn' : 'success', status === 'Retired' ? 'Plan retired' : 'Plan reactivated',
            status === 'Retired' ? `${plan.name} can no longer be sold; existing licences keep resolving.` : `${plan.name} is back on sale.`);
          loadSummary();
          if(after) after();
        } catch (err) {
          toast('error','Could not change the plan', errText(err));
        }
      }

      function deletePlan(plan, after){
        if(!allowed('owner','Deleting a plan requires the owner role.')) return;
        confirmModal({
          title:'Delete Plan', confirmLabel:'Delete', danger:true,
          msg:`Delete "${plan.name}"? Only a plan no licence references can be removed — retire it instead if it has been sold.`,
          onConfirm: async () => {
            try {
              await Store.mutate(() => API.licensing.plans.remove(plan.id), { event:'licensing:changed' });
              toast('success','Plan deleted', plan.name);
              loadSummary();
              if(after) after();
            } catch (err) {
              toast('error','Could not delete the plan', errText(err));
            }
          },
        });
      }

      document.getElementById('liNew').addEventListener('click', () => editPlan(null, () => { if(activeTab === 0) goTab(0); }));

      // ---- tab 1: tenants & licenses ----------------------------------------
      function tabTenants(){
        body.innerHTML = `<div id="liTenantsTbl"></div>`;
        const t = dataTable({
          columns:[
            { key:'tenant', label:'Tenant', render:r => entityCell(r.tenant_name || r.tenant_workspace_id, r.purchase_order_ref || null, 'globe', 'blue') },
            { key:'plan', label:'Plan', render:r => badge(r.plan_name || '—', tierColor[r.plan_tier] || 'gray') },
            { key:'seats_purchased', label:'Seats', align:'right', cls:'num',
              render:r => `${fmtFull(r.seats_assigned)} / ${fmtFull(r.seats_purchased)}` },
            { key:'tokens', label:'Token Capacity', align:'right', cls:'num', sortable:false, render:r => num(r.included_tokens) },
            { key:'runs', label:'Run Capacity', align:'right', cls:'num', sortable:false, render:r => num(r.included_runs) },
            { key:'status', label:'Status', render:r => statusText(r.status, licColor(r.status)) },
            { key:'expires_at', label:'Renewal', render:r => r.expires_at
                ? `<span class="dim nowrap">${day(r.expires_at)}${r.days_until_expiry == null ? '' : ` · ${r.days_until_expiry}d`}</span>`
                : '<span class="faint">no end date</span>' },
            { key:'manage', label:'', sortable:false, render:r => `<button class="btn sm" data-ten="${esc(r.id)}">Manage</button>` },
          ],
          rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'tenant licenses',
          searchPlaceholder:'Search tenants…',
          emptyText:'No tenant licence has been issued for this workspace yet',
          filters:[
            { key:'status', label:'Status', param:'status', options:LICENSE_STATUS, allLabel:'All Status' },
            { key:'tier', label:'Tier', param:'tier', options:PLAN_TIERS, allLabel:'All Tiers' },
          ],
          source: (params) => API.licensing.tenants.list(params),
          onLoad: () => bindManage(),
          onSelect: (row) => manageTenant(row, () => t.refresh()),
        });
        activeTable = t;
        const holder = document.getElementById('liTenantsTbl');
        holder.appendChild(t.filterEl);
        holder.appendChild(t.el);

        function bindManage(){
          t.el.querySelectorAll('[data-ten]').forEach(btn => {
            btn.addEventListener('click', (e) => {
              e.stopPropagation();
              const row = t.getRows().find(x => x.id === btn.dataset.ten);
              if(row) manageTenant(row, () => t.refresh());
            });
          });
        }

        if(!Store.session.can('owner')){
          const note = document.createElement('div');
          note.className = 'small faint mt';
          note.textContent = 'Issuing and amending a licence requires the owner role.';
          holder.appendChild(note);
        } else {
          const btn = document.createElement('button');
          btn.className = 'btn sm primary mt';
          btn.innerHTML = ICONS.plus + 'Issue License';
          btn.addEventListener('click', () => issueLicense(() => { licenseLoaded = false; t.refresh(); loadSummary(); }));
          holder.appendChild(btn);
        }
      }

      async function issueLicense(after){
        if(!allowed('owner','Issuing a licence requires the owner role.')) return;
        let plans = { items: [] };
        try { plans = await API.licensing.plans.list({ page_size: 100, status:'Active' }); }
        catch (err) { toast('error','Could not load plans', errText(err)); return; }
        if(!plans.items.length){ toast('warn','No plan to sell','Publish an Active plan first.'); return; }
        openModal({
          title:'Issue Tenant License', icon:'globe',
          body:`<div class="form-row"><label>PLAN</label>
              <select class="filter-select w-100" id="ilPlan" style="height:34px">${plans.items.map(p =>
                `<option value="${esc(p.id)}">${esc(p.name)} — ${esc(p.tier)} · ${money(p.price_per_seat_usd)}/seat</option>`).join('')}</select></div>
            <div class="grid g2">
              <div class="form-row"><label>STATUS</label>
                <select class="filter-select w-100" id="ilStatus" style="height:34px"><option>Active</option><option>Trial</option></select></div>
              <div class="form-row"><label>SEATS PURCHASED</label><input class="input" id="ilSeats" value="25"></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>EXPIRES</label><input class="input" id="ilExpires" type="date"></div>
              <div class="form-row"><label>PURCHASE ORDER</label><input class="input" id="ilPo" placeholder="PO-2026-001"></div>
            </div>
            <label class="flex" style="gap:8px;align-items:center;margin-top:4px">
              <input type="checkbox" id="ilRenew" checked><span class="small">Auto-renew</span></label>`,
          footer:[
            { label:'Cancel' },
            { label:'Issue License', cls:'primary', onClick: async (close, modal) => {
                const seats = parseInt(modal.querySelector('#ilSeats').value, 10);
                const expires = modal.querySelector('#ilExpires').value;
                const po = modal.querySelector('#ilPo').value.trim();
                const payload = {
                  plan_id: modal.querySelector('#ilPlan').value,
                  status: modal.querySelector('#ilStatus').value,
                  seats_purchased: isNaN(seats) ? 0 : seats,
                  auto_renew: modal.querySelector('#ilRenew').checked,
                };
                if(expires) payload.expires_at = new Date(expires + 'T00:00:00Z').toISOString();
                if(po) payload.purchase_order_ref = po;
                close();
                try {
                  const lic = await Store.mutate(() => API.licensing.tenants.create(payload), { event:'licensing:changed' });
                  toast('success','License issued', `${lic.plan_name || 'Plan'} · ${fmtFull(lic.seats_purchased)} seats.`);
                  licenseLoaded = false; license = lic;
                  loadSummary();
                  if(after) after();
                } catch (err) {
                  toast('error','Could not issue the licence', errText(err));
                }
              } },
          ],
        });
      }

      function manageTenant(lic, after){
        openModal({
          title:'Manage Tenant — ' + (lic.tenant_name || lic.tenant_workspace_id), icon:'globe', wide:true,
          body:`<div class="card-loading" style="height:180px"></div>`,
          footer:[{ label:'Close' }],
          onOpen(modal){
            const mb = modal.querySelector('.modal-body');
            API.licensing.tenants.get(lic.id)
              .then(l => {
                mb.innerHTML = kv([
                  ['Plan', `${esc(l.plan_name || '—')} ${badge(l.plan_tier || '—', tierColor[l.plan_tier] || 'gray')}`],
                  ['Status', statusText(l.status, licColor(l.status))],
                  ['Seats', `${fmtFull(l.seats_assigned)} assigned of ${fmtFull(l.seats_purchased)} · ${fmtFull(l.seats_available)} free`],
                  ['Seat Utilisation', l.seat_utilization_pct == null ? dash : barPct(l.seat_utilization_pct, l.seat_utilization_pct >= 85 ? 'amber' : 'green')],
                  ['Token Capacity', num(l.included_tokens)],
                  ['Run Capacity', num(l.included_runs)],
                  ['Term', `${day(l.starts_at)} → ${l.expires_at ? day(l.expires_at) : 'open-ended'}`],
                  ['Days to Renewal', l.days_until_expiry == null ? dash : String(l.days_until_expiry)],
                  ['Auto Renew', l.auto_renew ? badge('Yes','green') : badge('No','gray')],
                  ['Billing Contact', l.billing_contact_email ? esc(l.billing_contact_email) : dash],
                  ['Purchase Order', l.purchase_order_ref ? `<span class="mono">${esc(l.purchase_order_ref)}</span>` : dash],
                  ...(l.suspended_at ? [['Suspended', `${at(l.suspended_at)} — ${esc(l.suspended_reason || 'no reason recorded')}`]] : []),
                ]) + `<div class="quote mt">${ICONS.info} Changes to tenant entitlements take effect on the next entitlement check and are audited.</div>
                  <div class="flex mt" style="gap:8px;flex-wrap:wrap">
                    <button class="btn sm primary" id="mtUpgrade"${gate('owner','Changing a plan requires the owner role.')}>${ICONS.arrowUp}Change Plan</button>
                    <button class="btn sm" id="mtSeats"${gate('owner','Purchasing seats requires the owner role.')}>${ICONS.plus}Purchase Seats</button>
                    ${l.status === 'Suspended'
                      ? `<button class="btn sm success" id="mtReactivate"${gate('owner','Reactivating a licence requires the owner role.')}>${ICONS.checkCircle}Reactivate</button>`
                      : `<button class="btn sm danger" id="mtSuspend"${gate('owner','Suspending a licence requires the owner role.')}>${ICONS.xCircle}Suspend</button>`}
                    <button class="btn sm" id="mtUsage">${ICONS.gauge}Usage &amp; Limits</button>
                  </div>`;
                const up = mb.querySelector('#mtUpgrade');
                if(up) up.addEventListener('click', ()=>changePlan(l, after));
                const sb = mb.querySelector('#mtSeats');
                if(sb) sb.addEventListener('click', ()=>purchaseSeats(l, after));
                const su = mb.querySelector('#mtSuspend');
                if(su) su.addEventListener('click', ()=>suspendLicense(l, after));
                const re = mb.querySelector('#mtReactivate');
                if(re) re.addEventListener('click', ()=>reactivateLicense(l, after));
                mb.querySelector('#mtUsage').addEventListener('click', ()=>{ license = l; licenseLoaded = true; goTab(3); });
              })
              .catch(err => { mb.innerHTML = ''; mb.appendChild(screenError(err, null, 'this tenant licence')); });
          },
        });
      }

      async function changePlan(lic, after){
        if(!allowed('owner','Changing a plan requires the owner role.')) return;
        let plans = { items: [] };
        try { plans = await API.licensing.plans.list({ page_size: 100, status:'Active' }); }
        catch (err) { toast('error','Could not load plans', errText(err)); return; }
        openModal({
          title:'Change Plan', icon:'arrowUp',
          body:`<div class="form-row"><label>NEW PLAN</label>
              <select class="filter-select w-100" id="cpPlan" style="height:34px">${plans.items.map(p =>
                `<option value="${esc(p.id)}" ${p.id === lic.plan_id ? 'selected' : ''}>${esc(p.name)} — ${esc(p.tier)}</option>`).join('')}</select></div>
            <div class="form-row"><label>SEATS PURCHASED</label>
              <input class="input" id="cpSeats" value="${esc(String(lic.seats_purchased))}">
              <div class="small faint" style="margin-top:4px">Cannot drop below the ${fmtFull(lic.seats_assigned)} seat(s) already assigned.</div></div>`,
          footer:[
            { label:'Cancel' },
            { label:'Apply Change', cls:'primary', onClick: async (close, modal) => {
                const seats = parseInt(modal.querySelector('#cpSeats').value, 10);
                const payload = { plan_id: modal.querySelector('#cpPlan').value };
                if(!isNaN(seats)) payload.seats_purchased = seats;
                close();
                try {
                  const l = await Store.mutate(() => API.licensing.tenants.update(lic.id, payload), { event:'licensing:changed' });
                  toast('success','License updated', `${l.plan_name || 'Plan'} · ${fmtFull(l.seats_purchased)} seats.`);
                  license = l; licenseLoaded = true;
                  loadSummary();
                  if(after) after();
                } catch (err) {
                  toast('error','Could not change the plan', errText(err));
                }
              } },
          ],
        });
      }

      function purchaseSeats(lic, after){
        if(!allowed('owner','Purchasing seats requires the owner role.')) return;
        openModal({
          title:'Purchase Seats', icon:'plus',
          body:`<div class="quote">${esc(lic.plan_name || 'Licence')} — ${fmtFull(lic.seats_assigned)} of ${fmtFull(lic.seats_purchased)} seats in use.</div>
            <div class="grid g2 mt">
              <div class="form-row"><label>ADDITIONAL SEATS</label><input class="input" id="psSeats" value="10"></div>
              <div class="form-row"><label>PURCHASE ORDER</label><input class="input" id="psPo" placeholder="PO-2026-014"></div>
            </div>
            <div class="form-row"><label>NOTE</label><input class="input" id="psNote" placeholder="Why the pool is growing"></div>
            <div class="small faint">This changes what the tenant is billed and is audited with the before and after pool sizes.</div>`,
          footer:[
            { label:'Cancel' },
            { label:'Purchase', cls:'primary', onClick: async (close, modal) => {
                const seats = parseInt(modal.querySelector('#psSeats').value, 10);
                if(isNaN(seats) || seats <= 0){ toast('error','Cannot purchase','Enter how many seats to add.'); return; }
                const po = modal.querySelector('#psPo').value.trim();
                const note = modal.querySelector('#psNote').value.trim();
                const payload = { seats };
                if(po) payload.purchase_order_ref = po;
                if(note) payload.note = note;
                close();
                try {
                  const res = await Store.mutate(() => API.licensing.purchaseSeats(lic.id, payload), { event:'licensing:changed' });
                  const d = res.data || {};
                  toast('success','Seats purchased', res.message
                    || `${fmtFull(d.seats_purchased)} seats now on the licence, ${fmtFull(d.seats_available)} free.`);
                  licenseLoaded = false;
                  loadSummary();
                  if(after) after();
                } catch (err) {
                  toast('error','Could not purchase seats', errText(err));
                }
              } },
          ],
        });
      }

      function suspendLicense(lic, after){
        if(!allowed('owner','Suspending a licence requires the owner role.')) return;
        openModal({
          title:'Suspend License', icon:'xCircle',
          body:`<p style="margin:0 0 10px">New seat assignment is blocked from this moment and entitlement enforcement starts refusing.</p>
            <div class="form-row"><label>REASON</label><input class="input" id="slReason" placeholder="e.g. Non-payment"></div>
            <label class="flex" style="gap:8px;align-items:center">
              <input type="checkbox" id="slRelease"><span class="small">Also release the ${fmtFull(lic.seats_assigned)} seat(s) already held</span></label>`,
          footer:[
            { label:'Cancel' },
            { label:'Suspend', cls:'danger', onClick: async (close, modal) => {
                const reason = modal.querySelector('#slReason').value.trim();
                const release = modal.querySelector('#slRelease').checked;
                close();
                try {
                  const res = await Store.mutate(() => API.licensing.suspend(lic.id, { reason: reason || null, release_seats: release }),
                    { event:'licensing:changed' });
                  toast('warn','License suspended', res.message);
                  licenseLoaded = false;
                  loadSummary();
                  if(after) after();
                } catch (err) {
                  toast('error','Could not suspend', errText(err));
                }
              } },
          ],
        });
      }

      async function reactivateLicense(lic, after){
        if(!allowed('owner','Reactivating a licence requires the owner role.')) return;
        try {
          const res = await Store.mutate(() => API.licensing.reactivate(lic.id), { event:'licensing:changed' });
          toast('success','License reactivated', res.message);
          licenseLoaded = false;
          loadSummary();
          if(after) after();
        } catch (err) {
          toast('error','Could not reactivate', errText(err));
        }
      }

      // ---- tab 2: seats & assignments ---------------------------------------
      function tabSeats(){
        body.innerHTML = `<div class="card-loading" style="height:180px"></div>`;
        currentLicense()
          .then(l => {
            if(activeTab !== 2) return;
            if(!l){
              body.innerHTML = `<div class="card">${emptyBlock('users','No licence to hold seats',
                'Issue a tenant licence on the Tenants & Licenses tab, then seats can be assigned against it.')}</div>`;
              return;
            }
            body.innerHTML = `<div class="grid g2"><div id="liSeatsTbl"></div>
              <div class="card" id="liPoolCard"></div></div>`;
            const t = dataTable({
              columns:[
                { key:'user', label:'User', render:r => r.user_name || r.user_email
                    ? ownerCell(r.user_name || r.user_email, r.user_email || '') : `<span class="mono dim">${esc(String(r.user_id).slice(0,8))}…</span>` },
                { key:'email', label:'Email', render:r => r.user_email ? `<span class="dim">${esc(r.user_email)}</span>` : dash },
                { key:'role', label:'Seat Role', render:r => badge(r.role, 'purple') },
                { key:'assigned_at', label:'Assigned', render:r => when(r.assigned_at) },
                { key:'released_at', label:'State', render:r => r.is_active ? statusText('Active','green')
                    : statusText('Released','gray') + ` <span class="faint small">${esc(r.released_at ? relTime(ts(r.released_at)) : '')}</span>` },
                { key:'reassign', label:'', sortable:false, render:r => r.is_active
                    ? `<button class="btn sm" data-seat="${esc(r.user_id)}">Reassign</button>` : '' },
              ],
              rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'seats',
              searchPlaceholder:'Search seat holders…',
              emptyText:'No seat has been assigned on this licence yet',
              filters:[
                { key:'state', label:'State', param:'state', options:['active','released'], allLabel:'All Seats' },
                { key:'role', label:'Role', param:'role', options:SEAT_ROLES, allLabel:'All Roles' },
              ],
              source: (params) => API.licensing.seats(l.id, params),
              onLoad: () => bindSeats(),
            });
            activeTable = t;
            const holder = document.getElementById('liSeatsTbl');
            holder.appendChild(t.filterEl);
            holder.appendChild(t.el);
            paintPool(l);

            function bindSeats(){
              t.el.querySelectorAll('[data-seat]').forEach(btn => {
                btn.addEventListener('click', (e) => {
                  e.stopPropagation();
                  reassignSeat(l, btn.dataset.seat, () => { licenseLoaded = false; t.refresh(); currentLicense().then(paintPool); });
                });
              });
            }

            function paintPool(lc){
              const card = document.getElementById('liPoolCard');
              if(!card || !lc) return;
              const used = lc.seats_purchased ? (lc.seats_assigned / lc.seats_purchased) * 100 : 0;
              card.innerHTML = `<div class="card-head"><div class="card-title">Seat Pool</div>
                  <span class="small faint">${esc(lc.plan_name || '')}</span></div>
                <div style="margin-bottom:13px">
                  <div class="flex between" style="margin-bottom:4px"><b style="font-size:12.5px">${esc(lc.plan_tier || lc.plan_name || 'Licence')}</b>
                    <span class="num">${fmtFull(lc.seats_assigned)} / ${fmtFull(lc.seats_purchased)}</span></div>
                  <div class="bar-bg" style="height:6px"><div class="bar-fill" style="width:${Math.min(used,100)}%;background:${used > 85 ? 'var(--amber)' : 'var(--green)'}"></div></div>
                </div>
                ${kv([
                  ['Seats Available', num(lc.seats_available)],
                  ['Utilisation', lc.seat_utilization_pct == null ? dash : pct(lc.seat_utilization_pct)],
                  ['Licence Status', statusText(lc.status, licColor(lc.status))],
                ])}
                <button class="btn sm mt" id="seatAdd"${gate('owner','Purchasing seats requires the owner role.')}>${ICONS.plus}Purchase Seats</button>
                <button class="btn sm mt" id="seatAssign"${gate('admin','Assigning a seat requires the admin role.')}>${ICONS.users}Assign Seat</button>`;
              const add = card.querySelector('#seatAdd');
              if(add) add.addEventListener('click', ()=>purchaseSeats(lc, ()=>{ licenseLoaded = false; currentLicense().then(paintPool); t.refresh(); }));
              const asg = card.querySelector('#seatAssign');
              if(asg) asg.addEventListener('click', ()=>reassignSeat(lc, null, ()=>{ licenseLoaded = false; t.refresh(); currentLicense().then(paintPool); }));
            }
          })
          .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, tabSeats, 'the licence for this workspace')); });
      }

      async function reassignSeat(lic, replacesUserId, after){
        if(!allowed('admin','Assigning a seat requires the admin role.')) return;
        const all = await members.load();
        // A reassignment hands the seat to somebody else, so the person losing
        // it cannot be an option — the server refuses that pairing outright.
        const people = replacesUserId ? all.filter(u => u.id !== replacesUserId) : all;
        if(!people.length){
          toast('warn', replacesUserId ? 'Nobody to reassign to' : 'No members',
            replacesUserId
              ? 'This workspace has no other member who could take the seat. Invite one first.'
              : 'This workspace has nobody to give a seat to.');
          return;
        }
        openModal({
          title: replacesUserId ? 'Reassign Seat' : 'Assign Seat', icon:'users',
          body:`${replacesUserId ? `<div class="quote">Releasing the seat held by ${esc(members.name(replacesUserId) || replacesUserId)} and issuing it to somebody else, in one transaction.</div>` : ''}
            <div class="form-row mt"><label>NEW HOLDER</label>
              <select class="filter-select w-100" id="rsUser" style="height:34px">${people.map(u =>
                `<option value="${esc(u.id)}">${esc(u.full_name)}</option>`).join('')}</select></div>
            <div class="form-row"><label>SEAT ROLE</label>
              <select class="filter-select w-100" id="rsRole" style="height:34px">${SEAT_ROLES.map(r =>
                `<option ${r === 'member' ? 'selected' : ''}>${r}</option>`).join('')}</select></div>`,
          footer:[
            { label:'Cancel' },
            { label: replacesUserId ? 'Reassign' : 'Assign', cls:'primary', onClick: async (close, modal) => {
                const payload = { user_id: modal.querySelector('#rsUser').value, role: modal.querySelector('#rsRole').value };
                if(replacesUserId) payload.replaces_user_id = replacesUserId;
                close();
                try {
                  const seat = await Store.mutate(() => API.licensing.assignSeat(lic.id, payload), { event:'licensing:changed' });
                  toast('success', replacesUserId ? 'Seat reassigned' : 'Seat assigned',
                    `${seat.user_name || seat.user_email || 'The member'} now holds ${/^[aeiou]/i.test(seat.role || '') ? 'an' : 'a'} ${seat.role} seat.`);
                  loadSummary();
                  if(after) after();
                } catch (err) {
                  toast('error','Could not assign the seat', errText(err));
                }
              } },
          ],
        });
      }

      // ---- tab 3: usage & limits --------------------------------------------
      function tabUsage(){
        body.innerHTML = `<div class="card-loading" style="height:200px"></div>`;
        currentLicense()
          .then(l => {
            if(activeTab !== 3) return;
            if(!l){
              body.innerHTML = `<div class="card">${emptyBlock('gauge','No licence to meter',
                'Usage is measured against a tenant licence. Issue one to see allowances and consumption.')}</div>`;
              return;
            }
            return API.licensing.entitlements(l.id).then(e => {
              if(activeTab !== 3) return;
              const usage = e.usage || [];
              const over = e.over_limit_metrics || [];
              body.innerHTML = `<div class="grid g2">
                <div class="card">
                  <div class="card-head"><div class="card-title">Usage vs Limits — ${esc(e.tenant_name || 'this workspace')}</div>
                    <span class="small faint">Rolling ${e.usage_window_days} days</span></div>
                  ${usage.length ? usage.map(u => {
                    const p = u.utilization_pct == null ? null : Math.min(u.utilization_pct, 100);
                    return `<div style="margin-bottom:12px">
                      <div class="flex between" style="margin-bottom:4px"><b style="font-size:12.5px">${esc(u.metric)}</b>
                        <span class="num dim">${fmtFull(u.used)}${u.included == null ? '' : ' / ' + fmtFull(u.included)}</span></div>
                      <div class="bar-bg" style="height:6px"><div class="bar-fill" style="width:${p == null ? 0 : p}%;background:${
                        u.over_limit ? 'var(--red)' : p != null && p > 85 ? 'var(--amber)' : 'var(--green)'}"></div></div>
                      <div class="small faint" style="margin-top:3px">${u.utilization_pct == null ? 'No allowance set for this meter'
                        : `${u.utilization_pct.toFixed(1)}% used · ${fmtFull(u.remaining)} remaining`}${
                        u.amount_usd == null ? '' : ` · ${money(u.amount_usd)} billed`}</div></div>`;
                  }).join('') : emptyBlock('gauge','Nothing metered in this window','Usage appears once agents report against this licence.')}
                </div>
                <div class="card">
                  <div class="card-head"><div class="card-title">Overage Alerts</div></div>
                  ${over.length ? over.map(m => {
                    const u = usage.find(x => x.metric === m) || {};
                    return `<div class="flex" style="gap:9px;padding:7px 0;align-items:flex-start">
                      <span style="width:13px;display:inline-flex;color:#B91C1C;margin-top:2px">${ICONS.alert}</span>
                      <div><b style="font-size:12.5px">${esc(m)}</b>
                        <div class="small dim">${u.used == null ? 'Over its allowance.' :
                          `${fmtFull(u.used)} of ${fmtFull(u.included)} — ${u.utilization_pct == null ? '' : u.utilization_pct.toFixed(0) + '%'} of the allowance.`}</div></div></div>`;
                  }).join('') : emptyBlock('checkCircle','No meter is over its allowance','Every metered resource is inside the plan limit.')}
                  <div class="divider"></div>
                  <div class="card-title" style="margin-bottom:6px">Resolved Entitlements</div>
                  ${(e.entitlements || []).length
                    ? kv(e.entitlements.map(en => [en.key,
                        `${en.value_type === 'bool'
                          ? (en.value ? badge('Granted','green') : badge('Not granted','gray'))
                          : `<span class="num">${esc(String(en.value == null ? '—' : en.value))}</span>`}
                         ${en.hard_limit ? badge('hard limit','red') : ''}`]))
                    : `<div class="faint small">This plan carries no enforceable entitlements.</div>`}
                  ${(e.features || []).length ? `<div class="divider"></div>
                    <div class="card-title" style="margin-bottom:6px">Plan Features</div>
                    <div class="flex flex-wrap" style="gap:5px">${e.features.map(f => `<span class="tag">${esc(f)}</span>`).join('')}</div>` : ''}
                </div></div>`;
            });
          })
          .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, tabUsage, 'the usage and entitlements')); });
      }

      // ---- tab 4: billing & renewals ----------------------------------------
      function tabBilling(){
        body.innerHTML = `<div id="liInvTbl"></div><div id="liRenew" class="mt"></div>`;
        const t = dataTable({
          columns:[
            { key:'invoice_ref', label:'Invoice', render:r => `<span class="mono" style="color:var(--text)">${esc(r.invoice_ref)}</span>` },
            { key:'tenant', label:'Tenant', render:r => esc(r.tenant_name || '—') },
            { key:'plan', label:'Plan', render:r => r.plan_name ? badge(r.plan_name, 'gray') : dash },
            { key:'total_usd', label:'Amount', align:'right', cls:'num', render:r => money(r.total_usd) },
            { key:'overage_usd', label:'Overage', align:'right', cls:'num', sortable:false, render:r => money(r.overage_usd) },
            { key:'period', label:'Period', render:r => `<span class="dim nowrap">${day(r.period_start)} – ${day(r.period_end)}</span>` },
            { key:'status', label:'Status', render:r => statusText(r.status, invColor(r.status)) },
            { key:'due_at', label:'Due', render:r => r.due_at ? `<span class="dim nowrap">${day(r.due_at)}</span>` : dash },
            { key:'pdf', label:'', sortable:false, render:r => `<button class="btn sm" data-inv="${esc(r.id)}">${ICONS.download}PDF</button>` },
          ],
          rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'invoices',
          searchPlaceholder:'Search invoices…',
          defaultSort:{ key:'period_start', dir:-1 },
          emptyText:'No invoice has been raised against this workspace yet',
          filters:[
            { key:'status', label:'Status', param:'status', options:INVOICE_STATUS, allLabel:'All Status' },
          ],
          source: (params) => API.licensing.invoices(params),
          onLoad: () => {
            t.el.querySelectorAll('[data-inv]').forEach(btn => {
              btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const row = t.getRows().find(x => x.id === btn.dataset.inv);
                try {
                  const name = await API.licensing.invoice(btn.dataset.inv);
                  toast('success','Invoice downloaded', name || ((row && row.invoice_ref) || '') + '.pdf');
                } catch (err) {
                  toast('error','Could not download the invoice', errText(err));
                }
              });
            });
          },
        });
        activeTable = t;
        const holder = document.getElementById('liInvTbl');
        holder.appendChild(t.filterEl);
        holder.appendChild(t.el);
        paintRenewals();
      }

      function paintRenewals(){
        const host = document.getElementById('liRenew');
        if(!host) return;
        host.innerHTML = `<div class="card-loading" style="height:80px"></div>`;
        API.licensing.tenants.list({ page_size: 6, sort: 'expires_at' })
          .then(page => {
            const el = document.getElementById('liRenew');
            if(!el) return;
            const rows = (page.items || []).filter(l => l.expires_at);
            if(!rows.length){
              el.innerHTML = `<div class="card">${emptyBlock('calendar','No renewal dates recorded',
                'Licences without an end date renew nothing; set an expiry to track it here.')}</div>`;
              return;
            }
            el.innerHTML = `<div class="grid g3">${rows.slice(0,3).map(l => `<div class="card">
                <div class="small faint" style="font-weight:700">${esc(('Renewal — ' + (l.plan_name || l.tenant_name || 'Licence')).toUpperCase())}</div>
                <div style="font-size:18px;font-weight:700;margin-top:4px">${day(l.expires_at)}</div>
                <div class="small dim">${l.days_until_expiry == null ? '—' : `${l.days_until_expiry} days away`} · ${esc(l.status)}</div></div>`).join('')}</div>`;
          })
          .catch(err => {
            const el = document.getElementById('liRenew');
            if(el){ el.innerHTML = ''; el.appendChild(screenError(err, paintRenewals, 'the renewal dates')); }
          });
      }

      // ---- tab 5: audit log --------------------------------------------------
      function tabAudit(){
        body.innerHTML = `<div id="liAuditTbl"></div>`;
        const t = dataTable({
          columns:[
            { key:'occurred_at', label:'Time', render:r => `<span class="dim nowrap">${relTime(ts(r.occurred_at))}</span>` },
            { key:'actor', label:'Actor', render:r => r.actor ? ownerCell(r.actor, '') : dash },
            { key:'action', label:'Event', render:r => `<span class="cell-main">${esc(r.action)}</span>` },
            { key:'entity_type', label:'Entity', render:r => r.entity_label
                ? `${esc(r.entity_label)} <span class="faint small">${esc(r.entity_type || '')}</span>` : esc(r.entity_type || '—') },
            { key:'detail', label:'Detail', sortable:false, render:r => r.detail ? `<span class="dim">${esc(r.detail)}</span>` : dash },
          ],
          rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'audit events',
          searchPlaceholder:'Search the licensing audit trail…',
          defaultSort:{ key:'occurred_at', dir:-1 },
          emptyText:'Nothing has been changed on this screen yet',
          extraParams:{ source_screen:'Licensing & Entitlements' },
          source: (params) => API.audit.list(params),
        });
        activeTable = t;
        document.getElementById('liAuditTbl').appendChild(t.el);
      }

      goTab(0);
    },
  };

  /* ================= WORKSPACE SETTINGS ================= */
  /* Reached from the user menu, not the sidebar: your own profile, and the API
   * keys agents authenticate with. Minting shows the plaintext exactly once —
   * the server stores only a hash, so this screen never has a "show key" action. */

  const US_KPIS = ['Members','Privileged','Never Signed In','Added (30d)'];
  const ROLES = ['owner','admin','operator','approver','member','viewer'];
  const ROLE_COLORS = {
    owner:'purple', admin:'red', operator:'amber', approver:'blue', member:'green', viewer:'gray',
  };
  //: What each role may do, said in the words the server enforces.
  const ROLE_BLURB = {
    owner:'billing, licensing, workspace deletion',
    admin:'everything except billing',
    operator:'run, deploy and approve; no policy or secret edits',
    approver:'approve, reject and escalate only',
    member:'read, and author prompts, tests and feedback',
    viewer:'read only',
  };

  const AK_KPIS = ['Total Keys','Expiring Soon','Never Used','Agent-bound'];
  const AK_SCOPES = ['ingest','read','admin'];
  const AK_STATUS = ['Active','Expired','Revoked'];
  const AK_ENVIRONMENTS = ['Production','Staging','UAT','Development','QA','Sandbox','DR'];

  function akStatusColor(s){
    return { Active:'green', Expired:'gray', Revoked:'red' }[s] || 'gray';
  }

  SCREENS['settings'] = {
    title:'Workspace Settings',
    render(main, param){
      main.innerHTML = `
        ${pageHead({title:'Workspace Settings', sub:'Your profile, and the API keys agents use to report in.'})}
        <div id="wsTabs"></div>
        <div id="wsBody" class="mt"></div>`;

      const tabs = [{ label:'API Access Tokens' }, { label:'Users' }, { label:'Profile' }];
      const panels = [renderKeys, renderUsers, renderProfile];
      const initial = param === 'profile' ? 2 : param === 'users' ? 1 : 0;
      tabBar('wsTabs', tabs, (i) => panels[i](), initial);
      panels[initial]();

      // ---- profile ----------------------------------------------------------
      function renderProfile(){
        const body = document.getElementById('wsBody');
        const u = Store.session.user || {};
        const w = Store.session.workspace || {};
        body.innerHTML = `<div class="card" style="max-width:640px;padding:20px">
          ${kv([
            ['Name', esc(u.full_name || '—')],
            ['Email', `<span class="mono">${esc(u.email || '—')}</span>`],
            ['Job Title', u.job_title ? esc(u.job_title) : dash],
            ['Team', u.team ? esc(u.team) : dash],
            ['Role', badge(Store.session.role || '—', 'purple')],
            ['Workspace', `${esc(w.name || '—')} <span class="faint small mono">${esc(w.slug || '')}</span>`],
          ])}
          <div class="quote mt">${ICONS.info} Profile fields are managed by a workspace admin on the
          Users roster; this panel shows what the server holds for your session.</div>
        </div>`;
      }

      // ---- users ------------------------------------------------------------
      function renderUsers(){
        const body = document.getElementById('wsBody');
        body.innerHTML = `
          <div class="flex" style="justify-content:flex-end;gap:8px;margin-bottom:12px">
            ${searchBox('usSearch','Search people…')}
            <button class="btn primary" id="usNew"${gate('admin','Adding a member requires the admin role.')}>${ICONS.plus}Add Member</button>
          </div>
          <div id="usKpis">${kpiSkeleton(US_KPIS)}</div>
          <div id="usTableWrap" class="mt"></div>`;

        function loadSummary(){
          const host = document.getElementById('usKpis');
          if(!host) return;
          host.innerHTML = kpiSkeleton(US_KPIS);
          API.auth.users.summary()
            .then(s => {
              if(!document.getElementById('usKpis')) return;
              host.innerHTML = kpiRow([
                { label:'Members', value:num(s.total), icon:'users', color:'purple',
                  sub:`${num(s.active)} active · ${num(s.inactive)} inactive` },
                { label:'Privileged', value:num(s.privileged), icon:'shield',
                  color: s.privileged > s.total / 2 ? 'amber' : 'blue',
                  sub:`${num(s.owners)} owner(s) · ${num(s.admins)} admin(s)` },
                { label:'Never Signed In', value:num(s.never_signed_in), icon:'alert',
                  color: s.never_signed_in ? 'amber' : 'green',
                  sub:`${num(s.signed_in_last_30d)} active in 30 days` },
                { label:'Added (30d)', value:num(s.added_last_30d), icon:'plus', color:'cyan',
                  sub: s.last_joined_at ? `Last joined ${relTime(ts(s.last_joined_at))}` : 'Nobody joined yet' },
              ]);
            })
            .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSummary, 'the member summary')); });
        }

        const table = dataTable({
          columns:[
            { key:'full_name', label:'Person', render:r => ownerCell(r.full_name, r.email)
                + (r.is_current_user ? ' <span class="badge bg-purple">you</span>' : '') },
            { key:'role', label:'Role', render:r => badge(r.role, ROLE_COLORS[r.role] || 'gray') },
            { key:'job_title', label:'Job Title', render:r => r.job_title ? esc(r.job_title) : dash },
            { key:'team', label:'Team', render:r => r.team ? esc(r.team) : dash },
            { key:'status', label:'Status', sortable:false,
              render:r => statusText(r.status, r.status === 'Active' ? 'green' : 'gray') },
            { key:'password_set', label:'Sign-in', sortable:false,
              render:r => r.password_set
                ? '<span class="st-green">can sign in</span>'
                : '<span class="st-amber" title="No password has been set, so this account cannot sign in yet">no password</span>' },
            { key:'last_login_at', label:'Last Seen',
              render:r => r.last_login_at ? when(r.last_login_at) : '<span class="faint">never</span>' },
            { key:'joined_at', label:'Joined', render:r => when(r.joined_at) },
          ],
          rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'members',
          searchPlaceholder:'Search people…',
          defaultSort:{ key:'joined_at', dir:-1 },
          emptyText:'Nobody else has been added to this workspace yet',
          filters:[
            { key:'role', label:'Role', param:'role', options:ROLES, allLabel:'All Roles' },
            { key:'status', label:'Status', param:'status', options:['Active','Inactive'], allLabel:'All Status' },
          ],
          source: (params) => API.auth.users.list(params),
          exportSource: (params) => API.auth.users.export(params),
          rowActions: r => [
            { label:'Change Role', icon:'shield', onClick:()=>changeRole(r) },
            { label:'Set Password', icon:'key', onClick:()=>setPassword(r) },
            { sep:true },
            { label:'Remove From Workspace', icon:'trash', danger:true, onClick:()=>removeMember(r) },
          ],
        });

        const wrap = document.getElementById('usTableWrap');
        wrap.appendChild(table.filterEl);
        wrap.appendChild(table.el);
        document.getElementById('usSearch').addEventListener('input', e => table.search(e.target.value));
        document.getElementById('usNew').addEventListener('click', addMember);
        loadSummary();

        function refreshAll(){ table.refresh(); loadSummary(); }

        /** The role select, with owner offered only to an owner. */
        function roleOptions(selected){
          return ROLES
            .filter(role => role !== 'owner' || Store.session.can('owner'))
            .map(role => `<option value="${role}" ${role === selected ? 'selected' : ''}>${role} — ${esc(ROLE_BLURB[role])}</option>`)
            .join('');
        }

        function addMember(){
          if(!allowed('admin','Adding a member requires the admin role.')) return;
          openModal({
            title:'Add Member', icon:'plus',
            body:`<div class="grid g2">
                <div class="form-row"><label>FULL NAME</label>
                  <input class="input" id="usName" placeholder="e.g. Dana Rivers"></div>
                <div class="form-row"><label>WORK EMAIL</label>
                  <input class="input" id="usEmail" type="email" placeholder="dana@company.com"></div>
              </div>
              <div class="grid g2">
                <div class="form-row"><label>JOB TITLE</label>
                  <input class="input" id="usTitle" placeholder="e.g. Platform Engineer"></div>
                <div class="form-row"><label>TEAM</label>
                  <input class="input" id="usTeam" placeholder="e.g. Support Automation"></div>
              </div>
              <div class="form-row"><label>ROLE</label>
                <select class="filter-select w-100" id="usRole" style="height:34px">${roleOptions('member')}</select></div>
              <div class="form-row"><label>INITIAL PASSWORD</label>
                <input class="input" id="usPassword" type="text" placeholder="Leave blank to create the account without sign-in">
                <div class="small faint" style="margin-top:4px">Shown in clear so you can pass it on. They can sign in with it
                  immediately — tell them to change it. An address the platform already knows is joined to this workspace
                  instead, and keeps its existing password.</div></div>`,
            footer:[
              { label:'Cancel' },
              { label:'Add Member', cls:'primary', onClick: async (close, modal) => {
                  const full_name = modal.querySelector('#usName').value.trim();
                  const email = modal.querySelector('#usEmail').value.trim();
                  if(!full_name || !email){ toast('error','Cannot add','A member needs a name and an email address.'); return; }
                  const password = modal.querySelector('#usPassword').value;
                  const payload = {
                    full_name, email,
                    role: modal.querySelector('#usRole').value,
                    job_title: modal.querySelector('#usTitle').value.trim() || null,
                    team: modal.querySelector('#usTeam').value.trim() || null,
                  };
                  if(password) payload.password = password;
                  close();
                  try {
                    const member = await Store.mutate(() => API.auth.users.create(payload), { event:'members:changed' });
                    toast('success','Member added',
                      member.password_set
                        ? `${member.full_name} can sign in as ${member.email}.`
                        : `${member.full_name} was added without a password and cannot sign in yet.`);
                    refreshAll();
                  } catch (err) {
                    toast('error','Could not add the member', errText(err));
                  }
                } },
            ],
          });
        }

        function changeRole(r){
          if(!allowed('admin','Changing a role requires the admin role.')) return;
          openModal({
            title:'Change Role — ' + r.full_name, icon:'shield',
            body:`<div class="form-row"><label>ROLE</label>
                <select class="filter-select w-100" id="usNewRole" style="height:34px">${roleOptions(r.role)}</select></div>
              <div class="form-row"><label>REASON (OPTIONAL)</label>
                <input class="input" id="usRoleReason" placeholder="Recorded on the audit trail"></div>
              ${r.is_current_user ? `<div class="quote">${ICONS.alert} This is your own account. Lowering your
                role takes effect immediately and you may lose access to this screen.</div>` : ''}`,
            footer:[
              { label:'Cancel' },
              { label:'Change Role', cls:'primary', onClick: async (close, modal) => {
                  const role = modal.querySelector('#usNewRole').value;
                  const reason = modal.querySelector('#usRoleReason').value.trim();
                  if(role === r.role){ toast('info','No change', `${r.full_name} is already ${role}.`); close(); return; }
                  close();
                  try {
                    await Store.mutate(() => API.auth.users.setRole(r.id, reason ? { role, reason } : { role }),
                      { event:'members:changed' });
                    toast('success','Role changed', `${r.full_name} is now ${role}.`);
                    refreshAll();
                  } catch (err) {
                    toast('error','Could not change the role', errText(err));
                  }
                } },
            ],
          });
        }

        function setPassword(r){
          if(!allowed('admin','Setting a password requires the admin role.')) return;
          openModal({
            title:'Set Password — ' + r.full_name, icon:'key',
            body:`<div class="form-row"><label>NEW PASSWORD</label>
                <input class="input" id="usPw" type="text" placeholder="At least 12 characters">
                <div class="small faint" style="margin-top:4px">Shown in clear so you can pass it on. It replaces
                  any existing password for ${esc(r.email)}.</div></div>`,
            footer:[
              { label:'Cancel' },
              { label:'Set Password', cls:'primary', onClick: async (close, modal) => {
                  const password = modal.querySelector('#usPw').value;
                  if(!password){ toast('error','Cannot set','Type a password first.'); return; }
                  close();
                  try {
                    await Store.mutate(() => API.auth.users.update(r.id, { password }), { event:'members:changed' });
                    toast('success','Password set', `${r.full_name} can sign in with it now.`);
                    refreshAll();
                  } catch (err) {
                    toast('error','Could not set the password', errText(err));
                  }
                } },
            ],
          });
        }

        function removeMember(r){
          if(!allowed('admin','Removing a member requires the admin role.')) return;
          if(r.is_current_user){ toast('error','Not permitted','You cannot remove your own membership.'); return; }
          confirmModal({
            title:'Remove From Workspace', confirmLabel:'Remove', danger:true,
            msg:`Remove ${r.full_name} (${r.email}) from this workspace? They lose access immediately. `
              + `The person's account survives — this removes their membership here, not their identity.`,
            onConfirm: async () => {
              try {
                await Store.mutate(() => API.auth.users.remove(r.id), { event:'members:changed' });
                toast('success','Member removed', `${r.full_name} no longer has access.`);
                refreshAll();
              } catch (err) {
                toast('error','Could not remove the member', errText(err));
              }
            },
          });
        }
      }

      // ---- API keys ---------------------------------------------------------
      function renderKeys(){
        const body = document.getElementById('wsBody');
        body.innerHTML = `
          <div class="flex" style="justify-content:flex-end;gap:8px;margin-bottom:12px">
            ${searchBox('akSearch','Search keys…')}
            <button class="btn primary" id="akNew"${gate('admin','Minting an API key requires the admin role.')}>${ICONS.plus}Issue Key</button>
          </div>
          <div id="akKpis">${kpiSkeleton(AK_KPIS)}</div>
          <div id="akTableWrap" class="mt"></div>`;

        function loadSummary(){
          const host = document.getElementById('akKpis');
          if(!host) return;
          host.innerHTML = kpiSkeleton(AK_KPIS);
          API.auth.apiKeys.summary()
            .then(s => {
              if(!document.getElementById('akKpis')) return;
              host.innerHTML = kpiRow([
                { label:'Total Keys', value:num(s.total), icon:'key', color:'purple',
                  sub:`${num(s.active)} active · ${num(s.revoked)} revoked · ${num(s.expired)} expired` },
                { label:'Expiring Soon', value:num(s.expiring_soon), icon:'clock',
                  color: s.expiring_soon ? 'amber' : 'green',
                  sub: s.expiring_soon ? 'Rotate before they lapse' : 'Nothing lapsing soon' },
                { label:'Never Used', value:num(s.never_used), icon:'alert',
                  color: s.never_used ? 'amber' : 'cyan',
                  sub:`${num(s.used_last_7d)} used in the last 7 days` },
                { label:'Agent-bound', value:num(s.agent_bound), icon:'bot', color:'blue',
                  sub: s.last_used_at ? `Last call ${relTime(ts(s.last_used_at))}` : 'No key has been used yet' },
              ]);
            })
            .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSummary, 'the key summary')); });
        }

        const table = dataTable({
          columns:[
            { key:'name', label:'Key', render:r => entityCell(r.name, r.display_hint, 'key', 'purple') },
            { key:'scopes', label:'Scopes', sortable:false,
              render:r => (r.scopes || []).map(s => badge(s, s === 'admin' ? 'red' : s === 'ingest' ? 'blue' : 'gray')).join(' ') },
            { key:'agent_name', label:'Bound Agent', sortable:false,
              render:r => r.agent_name ? esc(r.agent_name) : `<span class="faint" title="An unbound key may report for any agent its scopes allow">any</span>` },
            { key:'environment', label:'Environment', render:r => r.environment ? esc(r.environment) : dash },
            // Status is derived from revoked_at/expires_at, not stored, so the
            // server cannot sort on it.
            { key:'status', label:'Status', sortable:false, render:r => statusText(r.status, akStatusColor(r.status)) },
            { key:'last_used_at', label:'Last Used',
              render:r => r.last_used_at ? `<span title="${esc(r.last_used_ip || '')}">${when(r.last_used_at)}</span>` : `<span class="faint">never</span>` },
            { key:'expires_at', label:'Expires',
              render:r => r.expires_at ? until(r.expires_at) : `<span class="faint" title="Expiry is strongly preferred">never</span>` },
            { key:'created_at', label:'Created', render:r => when(r.created_at) },
          ],
          rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'keys',
          searchPlaceholder:'Search keys…',
          defaultSort:{ key:'created_at', dir:-1 },
          emptyText:'No API keys have been issued in this workspace yet',
          filters:[
            { key:'status', label:'Status', param:'status', options:AK_STATUS, allLabel:'All Status' },
            { key:'scope', label:'Scope', param:'scope', options:AK_SCOPES, allLabel:'All Scopes' },
            { key:'environment', label:'Environment', param:'environment', options:AK_ENVIRONMENTS, allLabel:'All Environments' },
          ],
          source: (params) => API.auth.apiKeys.list(params),
          exportSource: (params) => API.auth.apiKeys.export(params),
          onSelect: (row) => showUsage(row),
          rowActions: r => [
            { label:'View Usage', icon:'activity', onClick:()=>showUsage(r) },
            { sep:true },
            { label: r.status === 'Revoked' ? 'Revoked' : 'Revoke Key', icon:'x', danger:true, onClick:()=>revoke(r) },
          ],
        });

        const wrap = document.getElementById('akTableWrap');
        wrap.appendChild(table.filterEl);
        wrap.appendChild(table.el);
        document.getElementById('akSearch').addEventListener('input', e => table.search(e.target.value));
        document.getElementById('akNew').addEventListener('click', mint);
        loadSummary();

        function refreshAll(){ table.refresh(); loadSummary(); }

        function showUsage(r){
          openModal({
            title:'Usage — ' + r.name, icon:'activity', wide:true,
            body:`<div class="card-loading" style="height:160px"></div>`,
            footer:[{ label:'Close' }],
            onOpen(modal){
              const body = modal.querySelector('.modal-body');
              API.auth.apiKeys.usage(r.id)
                .then(u => {
                  const days = (u.daily || []).filter(d => d.calls > 0);
                  body.innerHTML = kv([
                    ['Status', statusText(u.status, akStatusColor(u.status))],
                    ['Total Calls (all time)', num(u.total_calls)],
                    [`Calls (last ${u.window_days}d)`, num(u.calls_in_window)],
                    ['Ingest Calls', num(u.ingest_calls)],
                    ['Records Ingested', num(u.ingest_records)],
                    ['First Seen', at(u.first_seen_at)],
                    ['Last Used', u.last_used_at ? `${at(u.last_used_at)} <span class="mono small faint">${esc(u.last_used_ip || '')}</span>` : dash],
                  ])
                  + ((u.by_action || []).length
                      ? `<div class="small faint mt">By operation (last ${u.window_days}d)</div>`
                        + hbars(u.by_action.map(a => ({ label:a.action, value:a.count, display:fmtFull(a.count), color:'blue' })), { labelW:160 })
                      : `<div class="small faint mt">This key has not performed an audited operation in the window.</div>`)
                  + (days.length
                      ? `<div class="small faint mt">Active days</div>`
                        + hbars(days.slice(-10).map(d => ({ label:d.date, value:d.calls, display:fmtFull(d.calls), color:'purple' })), { labelW:110 })
                      : '');
                })
                .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, null, 'the key usage')); });
            },
          });
        }

        function revoke(r){
          if(r.status === 'Revoked'){ toast('info','Already revoked', `${r.name} was revoked ${r.revoked_at ? relTime(ts(r.revoked_at)) : 'earlier'}.`); return; }
          if(!allowed('admin','Revoking an API key requires the admin role.')) return;
          openModal({
            title:'Revoke Key', icon:'x',
            body:`<div class="quote">${ICONS.alert} Revoking <b>${esc(r.name)}</b> stops every agent using it
                <b>immediately</b>. This cannot be undone — a replacement has to be minted and rolled out.</div>
              <div class="form-row mt"><label>REASON (OPTIONAL)</label>
                <input class="input" id="akRevokeReason" placeholder="e.g. rotated after the Q3 audit"></div>`,
            footer:[
              { label:'Cancel' },
              { label:'Revoke Key', cls:'danger', onClick: async (close, modal) => {
                  const reason = modal.querySelector('#akRevokeReason').value.trim();
                  close();
                  try {
                    await Store.mutate(() => API.auth.apiKeys.revoke(r.id, reason ? { reason } : {}), { event:'api-keys:changed' });
                    toast('success','Key revoked', `${r.name} can no longer authenticate.`);
                    refreshAll();
                  } catch (err) {
                    toast('error','Could not revoke', errText(err));
                  }
                } },
            ],
          });
        }

        function mint(){
          if(!allowed('admin','Minting an API key requires the admin role.')) return;
          let agents = [];
          openModal({
            title:'Issue API Key', icon:'key',
            body:`<div class="form-row"><label>KEY NAME</label>
                <input class="input" id="akName" placeholder="e.g. billing-agent ingest"></div>
              <div class="form-row"><label>SCOPES</label>
                <div class="flex" style="gap:16px">
                  ${AK_SCOPES.map(s => `<label class="flex" style="gap:6px;align-items:center">
                    <input type="checkbox" data-scope="${s}" ${s === 'admin' ? '' : 'checked'}>
                    <span class="small">${s}</span></label>`).join('')}
                </div>
                <div class="small faint" style="margin-top:4px">ingest writes telemetry, read queries it,
                  admin can auto-register unknown agents — grant it rarely.</div></div>
              <div class="grid g2">
                <div class="form-row"><label>BIND TO AGENT</label>
                  <select class="filter-select w-100" id="akAgent" style="height:34px">
                    <option value="">Any agent (unbound)</option></select>
                  <div class="small faint" style="margin-top:4px">A bound key can only report that agent's telemetry.</div></div>
                <div class="form-row"><label>ENVIRONMENT</label>
                  <select class="filter-select w-100" id="akEnv" style="height:34px">
                    <option value="">Not pinned</option>
                    ${AK_ENVIRONMENTS.map(e => `<option>${e}</option>`).join('')}</select></div>
              </div>
              <div class="form-row"><label>EXPIRES IN (DAYS)</label>
                <input class="input" id="akTtl" placeholder="e.g. 90 — empty for a non-expiring key" style="max-width:280px">
                <div class="small faint" style="margin-top:4px">Expiry is strongly preferred; the maximum is 730 days.</div></div>`,
            footer:[
              { label:'Cancel' },
              { label:'Mint Key', cls:'primary', onClick: async (close, modal) => {
                  const name = modal.querySelector('#akName').value.trim();
                  if(!name){ toast('error','Cannot mint','The key needs a name.'); return; }
                  const scopes = Array.from(modal.querySelectorAll('[data-scope]'))
                    .filter(cb => cb.checked).map(cb => cb.dataset.scope);
                  if(!scopes.length){ toast('error','Cannot mint','Pick at least one scope.'); return; }
                  const ttlRaw = modal.querySelector('#akTtl').value.trim();
                  const ttl = ttlRaw ? parseInt(ttlRaw, 10) : null;
                  if(ttlRaw && (isNaN(ttl) || ttl < 1)){ toast('error','Cannot mint','Expiry must be a whole number of days.'); return; }
                  const payload = { name, scopes };
                  const agent = modal.querySelector('#akAgent').value;
                  const env = modal.querySelector('#akEnv').value;
                  if(agent) payload.agent_id = agent;
                  if(env) payload.environment = env;
                  if(ttl) payload.expires_in_days = ttl;
                  close();
                  try {
                    const created = await Store.mutate(() => API.auth.apiKeys.create(payload), { event:'api-keys:changed' });
                    refreshAll();
                    showToken(created);
                  } catch (err) {
                    toast('error','Could not mint the key', errText(err));
                  }
                } },
            ],
            onOpen(modal){
              API.agents.list({ page_size: 100, sort: 'name' })
                .then(page => {
                  agents = page.items || [];
                  const sel = modal.querySelector('#akAgent');
                  if(!sel) return;
                  agents.forEach(a => {
                    const o = document.createElement('option');
                    o.value = a.id;
                    o.textContent = `${a.name} (${a.environment})`;
                    sel.appendChild(o);
                  });
                })
                .catch(() => { /* the picker stays "any agent"; binding is optional */ });
            },
          });
        }

        function showToken(created){
          const copyBtn = (id, value) =>
            `<button class="btn sm" data-copy="${esc(value)}" id="${id}">${ICONS.copy || ICONS.clipboard || ''}Copy</button>`;
          openModal({
            title:'Key minted — copy it now', icon:'key', wide:true,
            body:`<div class="quote">${ICONS.alert} ${esc(created.warning || 'Store this key now. It cannot be shown again.')}</div>
              <div class="form-row mt"><label>${esc(created.name)}</label>
                <div class="flex" style="gap:8px;align-items:center">
                  <input class="input mono" id="akToken" readonly value="${esc(created.token)}" style="flex:1">
                  ${copyBtn('akCopyToken', created.token)}
                </div></div>
              ${(created.snippets || []).map((s, i) => `
                <div class="form-row"><label>${esc(s.label)}</label>
                  <div class="flex" style="gap:8px;align-items:flex-start">
                    <textarea class="input mono small" readonly rows="${Math.min(6, (s.code.match(/\n/g) || []).length + 1)}" style="flex:1;font-size:12px">${esc(s.code)}</textarea>
                    ${copyBtn('akCopySnip' + i, s.code)}
                  </div></div>`).join('')}`,
            footer:[{ label:'Done' }],
            onOpen(modal){
              modal.querySelectorAll('[data-copy]').forEach(btn => {
                btn.addEventListener('click', () => {
                  navigator.clipboard.writeText(btn.dataset.copy)
                    .then(() => toast('success','Copied','It is on your clipboard — store it somewhere safe.'))
                    .catch(() => toast('error','Could not copy','Select the text and copy it manually.'));
                });
              });
              const tok = modal.querySelector('#akToken');
              if(tok) tok.addEventListener('focus', () => tok.select());
            },
          });
        }
      }
    },
  };
})();
