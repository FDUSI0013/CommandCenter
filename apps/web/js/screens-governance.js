/* Fulcrum Ops — AGENT GOVERNANCE screens.
 *
 *   Connection Center · Agent Registry · Agent Detail ·
 *   Connector & MCP Governance · Policy Center · Approvals & Audit
 *
 * These are the screens that decide what an agent is allowed to do, so nothing
 * here may be approximated: every count, badge and row is what the control
 * plane returned for this workspace. A field the server did not send renders as
 * a dash, a call that failed says so and offers itself again, and an empty
 * table says the workspace is empty rather than filling itself in.
 */
(function(){
  'use strict';
  window.SCREENS = window.SCREENS || {};
  const { esc, relTime, fmtDate, fmtDateTime, fmtTime, fmtFull, fmtMoney, donut, gaugeRing } = U;
  const { badge, statusText, riskBadge, avatarHtml, ownerCell, entityCell, platformCell, kpiRow,
          kpiSkeleton, dataTable, pageHead, searchBox, tabBar, inspSection, kv, toast, openModal,
          confirmModal, screenError } = C;

  /* ---------------------------------------------------------------- values */

  const dash = '<span class="faint">—</span>';
  const ts = (v) => v ? new Date(v).getTime() : null;
  const num = (v) => v == null ? dash : fmtFull(v);
  const secs = (v) => v == null ? dash : Number(v).toFixed(2) + 's';
  const msecs = (v) => v == null ? dash : Math.round(v) + ' ms';
  const pct = (v, digits) => v == null ? dash : Number(v).toFixed(digits == null ? 1 : digits) + '%';
  const rel = (v) => v ? relTime(ts(v)) : dash;
  const when = (v) => v ? fmtDateTime(ts(v)) : dash;
  const day = (v) => v ? fmtDate(ts(v)) : dash;
  const text = (v) => (v == null || v === '') ? dash : esc(v);
  const dim = (v) => (v == null || v === '') ? dash : `<span class="dim">${esc(v)}</span>`;

  /** Up/down only when the server actually reported a movement. */
  function deltaDir(v){ return v == null || v === 0 ? null : (v > 0 ? 'up' : 'down'); }

  /** Seconds → the "2h 34m" shape the approval KPIs use. */
  function duration(sec){
    if(sec == null) return '—';
    const s = Math.round(sec);
    if(s < 60) return s + 's';
    const m = Math.floor(s/60), h = Math.floor(m/60);
    if(h >= 1) return `${h}h ${m % 60}m`;
    return `${m}m ${s % 60}s`;
  }

  /* ----------------------------------------------------------- permissions */

  /**
   * Disable a control the signed-in role may not use and say why on hover.
   * The server enforces the same rule; this is the UI being honest in advance.
   */
  function requireRole(el, role, what){
    if(!el) return el;
    if(Store.session.can(role)) return el;
    el.disabled = true;
    el.title = `${what || 'This action'} requires the ${role} role — you are signed in as ${Store.session.role || 'a viewer'}.`;
    el.classList.add('disabled');
    return el;
  }
  function denied(role, what){
    toast('error','Not permitted', `${what || 'This action'} requires the ${role} role.`);
  }
  function allowed(role, what){
    if(Store.session.can(role)) return true;
    denied(role, what);
    return false;
  }

  /* -------------------------------------------------------------- fragments */

  const EMPTY = (icon, title, body) =>
    `<div class="empty-state">${ICONS[icon] || ICONS.search}<div class="es-title">${esc(title)}</div><div>${body || ''}</div></div>`;

  function logoFor(key){ return LOGOS[key] || LOGOS.custom; }

  /** A connection's kind decides its mark when the server did not name one. */
  const KIND_LOGO = { 'Azure AI Foundry':'foundry', 'Copilot Studio':'copilot', 'M365 Copilot':'m365',
    'Microsoft 365':'m365', 'Power Platform':'power', 'MCP Server':'mcp', 'Vector Database':'vector',
    'Microsoft Purview':'purview', 'Custom REST API':'custom', 'Azure Key Vault':'keyvault',
    'SharePoint':'sharepoint', 'SQL Database':'sql', 'Azure Blob Storage':'azureblob' };

  const HEALTH_COLOR = { 'Healthy':'green', 'Warning':'amber', 'Unhealthy':'red' };
  const ACTIVITY_COLOR = { 'Success':'green', 'Warning':'amber', 'Error':'red' };

  const PLATFORMS = ['Azure AI Foundry','Copilot Studio','M365 Copilot','Power Platform','Custom Agent'];
  const ENVIRONMENTS = ['Production','Staging','UAT','Development','QA','Sandbox','DR'];
  const AGENT_TYPES = ['Pro-code','Low-code','Copilot'];
  const RISKS = ['Low','Medium','High'];
  const POLICY_STATUSES = ['Allowed','Warned','Blocked','Approval Required'];
  const CONNECTOR_TYPES = ['API','MCP Server','Database','Vector Store','Custom Tool','Connector','Data Source','Tool'];
  const CONNECTOR_STATUSES = ['Active','Blocked','Deprecated','Warning','Inactive'];
  const CLASSIFICATIONS = ['Public','Internal','External','Confidential','Restricted'];
  const AUTH_MODES = ['OAuth 2.0','API Key','Managed Identity','Service Principal'];
  const POLICY_CATEGORIES = ['Access Control','Guardrails','Data Protection','Approval & Escalation',
    'Routing & Orchestration','Usage & Quotas','Logging & Retention','Security','Compliance'];
  const POLICY_ENFORCEMENTS = ['Block','Require Approval','Escalate','Mask','Route','Throttle','Warn','Log Only'];
  const POLICY_STATES = ['Active','Inactive','Warning','Pending Review'];
  const POLICY_SCOPES = ['Global','Environment','Agent','Connector'];
  const APPROVAL_TRIGGERS = ['Financial action above threshold','Bulk data export','External communication','Production config change'];

  const CAT_COLORS = {'Access Control':'green','Guardrails':'purple','Data Protection':'blue',
    'Approval & Escalation':'pink','Routing & Orchestration':'cyan','Usage & Quotas':'amber',
    'Logging & Retention':'gray','Security':'red','Compliance':'blue'};
  const ENF_COLORS = {'Block':'red','Mask':'amber','Warn':'amber','Route':'cyan','Throttle':'orange',
    'Escalate':'pink','Require Approval':'purple','Log Only':'gray','Allow':'green'};

  function optionList(values, selected){
    return values.map(v=>`<option ${v===selected?'selected':''}>${esc(v)}</option>`).join('');
  }

  /** Fill a table's dropdown with options the server told us exist. */
  function fillFilter(table, index, values){
    const sel = table.filterEl && table.filterEl.querySelector(`[data-fi="${index}"]`);
    if(!sel) return;
    values.forEach(v=>{
      if(!v || Array.from(sel.options).some(o=>o.textContent === v)) return;
      const opt = document.createElement('option');
      opt.textContent = v;
      sel.appendChild(opt);
    });
  }

  /* ============================ CONNECTION CENTER ============================ */

  SCREENS['connections'] = {
    title:'Hosting & Deployment',
    render(main){
      let tiles = [], summary = null, donutMode = 'Health', searchTimer = null, query = '';

      main.innerHTML = `
        ${pageHead({title:'Hosting & Deployment', sub:'The platforms your agents are hosted on, the traffic flowing through them, and their health.',
          actions:`${searchBox('ccSearch','Search connections…')}
          <button class="btn" id="ccRefresh">${ICONS.refresh}Refresh All</button>
          <button class="btn primary" id="ccAdd">${ICONS.plus}Add Connection</button>`})}
        <div id="ccKpis">${kpiSkeleton(['Total Connections','Connected','Warning','Disconnected','Last Sync'])}</div>
        <div class="conn-grid" id="ccGrid"><div class="card card-loading" style="height:158px"></div><div class="card card-loading" style="height:158px"></div><div class="card card-loading" style="height:158px"></div></div>
        <div class="two-col">
          <div class="card pad-0" style="padding:16px 16px 0">
            <div class="card-head" style="margin-bottom:8px"><div class="card-title">Connection Activity</div>
            <button class="link" id="ccActAll">View All Activity ${ICONS.arrowRight}</button></div>
            <div id="ccActivity"><div class="card-loading" style="height:150px"></div></div>
          </div>
          <div style="display:flex;flex-direction:column;gap:14px">
            <div class="card"><div class="card-head"><div class="card-title">Connection Health Overview</div>
              <select class="filter-select" style="height:27px" id="ccDonutMode"><option>Health</option><option>Type</option></select></div>
              <div class="donut-wrap" id="ccDonut"><div class="card-loading" style="height:150px;width:100%"></div></div></div>
            <div class="card"><div class="card-head"><div class="card-title">Integration Quick Actions</div></div>
              <div class="grid g2" style="gap:9px">
                <button class="btn block" id="qaTest">${ICONS.activity}Test All Connections</button>
                <button class="btn block" id="qaSync">${ICONS.refresh}Sync All Connections</button>
                <button class="btn block" id="qaHealth">${ICONS.gauge}View System Health</button>
                <button class="btn block" id="qaHooks">${ICONS.link}Manage Webhooks</button>
              </div></div>
          </div>
        </div>`;

      requireRole(document.getElementById('ccAdd'), 'admin', 'Adding a connection');
      requireRole(document.getElementById('qaTest'), 'operator', 'Testing connections');
      requireRole(document.getElementById('qaSync'), 'operator', 'Syncing connections');
      const hooks = document.getElementById('qaHooks');
      hooks.disabled = true;
      hooks.title = 'The control plane does not expose a webhook registry yet, so there is nothing to manage here.';

      /* ---- KPI cards + the health donut, both off /connections/summary ---- */
      function loadSummary(){
        const host = document.getElementById('ccKpis');
        if(!host) return;
        host.innerHTML = kpiSkeleton(['Total Connections','Connected','Warning','Disconnected','Last Sync']);
        API.connections.summary()
          .then(s => {
            summary = s;
            if(!document.getElementById('ccKpis')) return;
            host.innerHTML = kpiRow([
              {label:'Total Connections', value:num(s.total), sub:`${num(s.enabled)} enabled · ${num(s.disabled)} disabled`, icon:'plug', color:'purple'},
              {label:'Connected', value:`<span class="st-green">${num(s.connected)}</span>`,
                sub: s.total && s.connected === s.total ? 'All systems operational' : `${num(s.linked_agents)} agents linked`, icon:'checkCircle', color:'green'},
              {label:'Warning', value:`<span class="st-amber">${num(s.warning)}</span>`, sub:s.warning?'Requires attention':'None', icon:'alert', color:'amber'},
              {label:'Disconnected', value:`<span class="st-red">${num(s.disconnected)}</span>`, sub:s.disconnected?'Connection failed':'None', icon:'wifiOff', color:'red'},
              {label:'Last Sync', value:`<span style="font-size:19px">${s.last_sync_at ? esc(relTime(ts(s.last_sync_at))) : '—'}</span>`,
                sub:`${num(s.syncs_today)} syncs today`, icon:'clock', color:'blue'},
            ]);
            paintDonut();
          })
          .catch(err => {
            host.innerHTML = '';
            host.appendChild(screenError(err, loadSummary, 'the connection summary'));
            const d = document.getElementById('ccDonut');
            if(d){ d.innerHTML = ''; d.appendChild(screenError(err, loadSummary, 'the health breakdown')); }
          });
      }

      function paintDonut(){
        const host = document.getElementById('ccDonut');
        if(!host || !summary) return;
        const slices = donutMode === 'Health'
          ? (summary.health_breakdown || []).map(h => ({label:h.health, value:h.count, percent:h.percent,
              color:HEALTH_COLOR[h.health] || 'gray'}))
          : kindSlices();
        const total = slices.reduce((a,s)=>a+s.value, 0);
        if(!total){
          host.innerHTML = EMPTY('plug','No connections to chart', donutMode === 'Health'
            ? 'Add a connection and its health appears here.'
            : 'Connection types appear once a connection is registered.');
          return;
        }
        host.innerHTML = `
          ${donut({segments:slices.map(s=>({value:s.value, color:s.color})), size:150, thickness:17,
                   centerVal:String(total), centerLabel:'Total'})}
          <div class="legend grow">
            ${slices.map(s=>`<div class="legend-item"><span class="sw" style="background:${U.cc(s.color)}"></span>
              <span class="lg-label">${esc(s.label)}</span><span class="lg-val">${s.value}</span>
              <span class="lg-pct">${(s.percent != null ? s.percent : (s.value/total*100)).toFixed(1)}%</span></div>`).join('')}
          </div>
          ${donutMode === 'Type' && summary.total > tiles.length
            ? `<div class="faint small" style="width:100%">Counted across the ${tiles.length} connections loaded above.</div>` : ''}`;
      }

      /** Type view: grouped from the connection rows the server returned. */
      function kindSlices(){
        const palette = ['purple','blue','green','amber','cyan','orange','pink','gray'];
        const counts = {};
        tiles.forEach(c => { counts[c.kind || 'Unspecified'] = (counts[c.kind || 'Unspecified'] || 0) + 1; });
        return Object.keys(counts).sort().map((k,i)=>({label:k, value:counts[k], color:palette[i % palette.length]}));
      }

      /* ---- the tiles ---- */
      function loadGrid(){
        const grid = document.getElementById('ccGrid');
        if(!grid) return;
        grid.innerHTML = `<div class="card card-loading" style="height:158px"></div><div class="card card-loading" style="height:158px"></div><div class="card card-loading" style="height:158px"></div>`;
        API.connections.list({ page_size: 60, q: query || undefined, sort: 'name' })
          .then(page => {
            if(!document.getElementById('ccGrid')) return;
            tiles = page.items || [];
            if(!tiles.length){
              grid.innerHTML = `<div class="card" style="grid-column:1/-1">${
                query ? EMPTY('search','No connection matches “'+esc(query)+'”','Clear the search to see every connection.')
                      : EMPTY('plug','No connections yet','Add a connection so agents on that platform can report in.')}</div>`;
              paintDonut();
              return;
            }
            grid.innerHTML = tiles.map(tile).join('');
            wireTiles();
            paintDonut();
          })
          .catch(err => { grid.innerHTML = ''; const box = document.createElement('div');
            box.style.gridColumn = '1/-1'; box.appendChild(screenError(err, loadGrid, 'the connections')); grid.appendChild(box); });
      }

      function tile(c){
        return `<div class="conn-card" data-conn="${esc(c.id)}">
          <div class="cc-head"><span class="cc-logo" style="background:var(--panel-3)">${logoFor(c.logo_key || KIND_LOGO[c.kind])}</span>
            <div class="grow"><div class="cc-name">${esc(c.name)}</div><div style="margin-top:3px">${badge(c.status, null, true)}</div></div>
            <button class="icon-btn" data-connmenu>${ICONS.dots}</button></div>
          <div>
            ${(c.metadata_pairs||[]).map(m=>`<div class="cc-kv"><span class="k">${esc(m[0])}</span><span class="v">${esc(m[1])}</span></div>`).join('')}
            <div class="cc-kv"><span class="k">Type</span><span class="v">${esc(c.kind || '—')}</span></div>
            <div class="cc-kv"><span class="k">Last Sync</span><span class="v">${c.last_sync_at ? esc(relTime(ts(c.last_sync_at))) : '—'}</span></div>
            <div class="cc-kv"><span class="k">Latency</span><span class="v">${c.latency_ms == null ? '—' : Math.round(c.latency_ms)+' ms'}</span></div>
            <div class="cc-kv"><span class="k">Health</span><span class="v">${statusText(c.health, HEALTH_COLOR[c.health])}</span></div>
          </div>
          <div class="cc-actions">
            <button class="btn sm" data-details>View Details</button>
            <button class="btn sm" data-test style="color:var(--purple-bright);border-color:rgba(124,92,252,.4)">Test Connection</button>
          </div></div>`;
      }

      function wireTiles(){
        document.querySelectorAll('#ccGrid .conn-card').forEach(card=>{
          const cn = tiles.find(c=>c.id === card.dataset.conn);
          if(!cn) return;
          card.querySelector('[data-details]').addEventListener('click', ()=>showDetails(cn));
          requireRole(card.querySelector('[data-test]'), 'operator', 'Testing a connection')
            .addEventListener('click', ()=>testConn(cn));
          card.querySelector('[data-connmenu]').addEventListener('click', e=>{
            C.openMenu(e.currentTarget, [
              {label:'View Details', icon:'eye', onClick:()=>showDetails(cn)},
              {label:'Tool Calls & Data', icon:'link', onClick:()=>showTraffic(cn)},
              {label:'Test Connection', icon:'activity', onClick:()=>testConn(cn)},
              {label:'Sync Now', icon:'refresh', onClick:()=>syncConn(cn)},
              {label:'Configure', icon:'settings', onClick:()=>configure(cn)},
              {sep:true},
              cn.enabled
                ? {label:'Disable Connection', icon:'xCircle', danger:true, onClick:()=>setEnabled(cn, false)}
                : {label:'Enable Connection', icon:'checkCircle', onClick:()=>setEnabled(cn, true)},
            ]);
          });
        });
      }

      /* ---- the activity feed ---- */
      function loadActivity(){
        const host = document.getElementById('ccActivity');
        if(!host) return;
        host.innerHTML = '<div class="card-loading" style="height:150px"></div>';
        API.connections.activity({ page_size: 8, sort: '-occurred_at' })
          .then(page => {
            if(!document.getElementById('ccActivity')) return;
            const rows = page.items || [];
            if(!rows.length){
              host.innerHTML = EMPTY('history','No connection activity yet','Tests, syncs and registrations are recorded here.');
              return;
            }
            host.innerHTML = `<div class="tbl-wrap"><table class="tbl">
              <thead><tr><th>Time</th><th>Connection</th><th>Event</th><th>Status</th><th>Details</th></tr></thead>
              <tbody>${rows.map(a=>`<tr style="cursor:default">
                <td class="dim nowrap">${esc(relTime(ts(a.occurred_at)))}</td>
                <td><span class="flex" style="gap:7px"><span style="width:16px;height:16px;display:inline-flex">${logoFor(a.connection_logo_key)}</span><b style="font-size:12px">${esc(a.connection_name || '—')}</b></span></td>
                <td>${esc(a.event)}</td>
                <td>${statusText(a.status, ACTIVITY_COLOR[a.status])}</td>
                <td class="dim">${esc(a.details || '—')}</td></tr>`).join('')}</tbody></table></div>`;
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadActivity, 'the activity feed')); });
      }

      /* ---- actions ---- */
      /* What has actually flowed through this connection.
       *
       * Health answers "does the endpoint reply". This answers "is anything
       * using it" - the tool calls and data operations reported by agents whose
       * platform matches this connection's kind. Nothing here is declared on
       * the connection: it is all observed.
       */
      function showTraffic(cn){
        openModal({
          title:'Tool Calls & Data — ' + cn.name, icon:'link', wide:true,
          body:`<div class="flex" style="gap:8px;align-items:center;margin-bottom:10px">
              <label class="small dim">Window</label>
              <select class="filter-select" id="ctWindow" style="height:30px">
                <option value="7">Last 7 days</option>
                <option value="30" selected>Last 30 days</option>
                <option value="90">Last 90 days</option>
              </select>
            </div>
            <div id="ctBody"><div class="card-loading" style="height:200px"></div></div>`,
          footer:[{label:'Close'}],
          onOpen(modal){
            const host = modal.querySelector('#ctBody');
            const picker = modal.querySelector('#ctWindow');
            const load = () => {
              host.innerHTML = '<div class="card-loading" style="height:200px"></div>';
              API.connections.traffic(cn.id, { window_days: picker.value })
                .then(t => {
                  const tools = t.tool_calls || [];
                  const flows = t.data_flows || [];
                  host.innerHTML = `
                    <div class="flex" style="gap:14px;flex-wrap:wrap;margin-bottom:12px">
                      ${kv([['Agents on this platform', String(t.agents)]])}
                      ${kv([['Runs in window', fmtFull(t.runs)]])}
                      ${kv([['Distinct tools', String(tools.length)]])}
                      ${kv([['Data operations', String(flows.length)]])}
                    </div>
                    ${t.truncated ? `<div class="quote small">${ICONS.info} The span scan hit its cap, so these totals are a floor rather than a complete count.</div>` : ''}
                    ${t.attributable === false ? `<div class="empty-state">${ICONS.link}
                        <div class="es-title">Traffic cannot be attributed to this connection</div>
                        <div>${esc(t.note || '')}</div></div>`
                      : !t.agents ? `<div class="empty-state">${ICONS.link}
                        <div class="es-title">No agents on this platform</div>
                        <div>Register an agent with platform "${esc(t.kind)}" and its traffic appears here.</div></div>`
                      : (!tools.length && !flows.length) ? `<div class="empty-state">${ICONS.link}
                        <div class="es-title">Nothing reported in this window</div>
                        <div>${t.agents} agent(s) run on this platform but reported no tool or data spans.
                        That is different from the connection being unhealthy.</div></div>` : `
                      ${tools.length ? `<div class="small faint" style="margin-bottom:6px">Tool calls</div>
                        <table class="tbl"><thead><tr><th>Tool</th><th class="num">Calls</th><th class="num">Errors</th><th class="num">Avg</th><th>Last called</th></tr></thead><tbody>
                        ${tools.map(x=>`<tr style="cursor:default"><td>${esc(x.name)}</td>
                          <td class="num">${fmtFull(x.calls)}</td>
                          <td class="num">${x.errors ? `<span class="st-red">${x.errors}</span>` : '0'}</td>
                          <td class="num">${x.avg_duration_ms != null ? Math.round(x.avg_duration_ms)+' ms' : dash}</td>
                          <td class="dim">${x.last_called_at ? relTime(ts(x.last_called_at)) : dash}</td></tr>`).join('')}
                        </tbody></table>` : ''}
                      ${flows.length ? `<div class="small faint" style="margin:14px 0 6px">Data &amp; retrieval</div>
                        <table class="tbl"><thead><tr><th>Operation</th><th class="num">Count</th><th class="num">Avg</th><th>Last seen</th></tr></thead><tbody>
                        ${flows.map(x=>`<tr style="cursor:default"><td>${esc(x.name)}</td>
                          <td class="num">${fmtFull(x.operations)}</td>
                          <td class="num">${x.avg_duration_ms != null ? Math.round(x.avg_duration_ms)+' ms' : dash}</td>
                          <td class="dim">${x.last_seen_at ? relTime(ts(x.last_seen_at)) : dash}</td></tr>`).join('')}
                        </tbody></table>` : ''}`}`;
                })
                .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, load, 'this connection traffic')); });
            };
            picker.addEventListener('change', load);
            load();
          },
        });
      }

      async function testConn(cn){
        if(!allowed('operator','Testing a connection')) return;
        toast('info','Testing '+cn.name+'…','Probing the endpoint.');
        try {
          const res = await Store.mutate(()=>API.connections.test(cn.id), { event:'connections:changed' });
          toast(res.ok ? 'success' : 'warn', res.ok ? cn.name+' responded' : cn.name+' — warning', res.message);
        } catch (err) {
          toast('error','Test failed', err.message);
        }
        reloadAll();
      }

      async function syncConn(cn){
        if(!allowed('operator','Syncing a connection')) return;
        try {
          const res = await Store.mutate(()=>API.connections.sync(cn.id), { event:'connections:changed' });
          toast(res.ok ? 'success' : 'warn','Sync '+(res.ok?'complete':'reported a problem'), res.message);
        } catch (err) {
          toast('error','Sync failed', err.message);
        }
        reloadAll();
      }

      async function setEnabled(cn, enabled){
        if(!allowed('admin', enabled ? 'Enabling a connection' : 'Disabling a connection')) return;
        try {
          await Store.mutate(()=>API.connections.update(cn.id, { enabled }), { event:'connections:changed' });
          toast(enabled ? 'success' : 'warn', enabled ? 'Connection enabled' : 'Connection disabled',
            `${cn.name} is now ${enabled ? 'enabled' : 'disabled'}.`);
          reloadAll();
        } catch (err) {
          toast('error','Could not update the connection', err.message);
        }
      }

      function configure(cn){
        if(!allowed('admin','Configuring a connection')) return;
        openModal({
          title:'Configure — '+cn.name, icon:'settings',
          body:`<div class="form-row"><label>DISPLAY NAME</label><input class="input" id="cfgName" value="${esc(cn.name)}"></div>
            <div class="form-row"><label>TYPE</label><input class="input" id="cfgKind" value="${esc(cn.kind||'')}"></div>
            <div class="form-row"><label>ENDPOINT / RESOURCE URI</label><input class="input" id="cfgEndpoint" value="${esc((cn.config||{}).endpoint_url||'')}" placeholder="https://…"></div>
            <div class="form-row"><label>NOTE</label><textarea class="input" id="cfgNote" rows="2">${esc(cn.note||'')}</textarea></div>`,
          footer:[{label:'Cancel'},{label:'Save Changes', cls:'primary', onClick: async (close, modal)=>{
            const endpoint = modal.querySelector('#cfgEndpoint').value.trim();
            if(endpoint && !/^https?:\/\//i.test(endpoint)){ toast('error','Check the endpoint','The endpoint must be an absolute http(s) URL.'); return; }
            const config = Object.assign({}, cn.config || {}, { endpoint_url: endpoint || null });
            // The server redacts secret values on read — echoing the markers back would store them.
            Object.keys(config).forEach(k=>{ if(config[k] === '***redacted***') delete config[k]; });
            const body = {
              name: modal.querySelector('#cfgName').value.trim() || cn.name,
              kind: modal.querySelector('#cfgKind').value.trim() || cn.kind,
              note: modal.querySelector('#cfgNote').value.trim() || null,
              config,
            };
            try {
              const saved = await Store.mutate(()=>API.connections.update(cn.id, body), { event:'connections:changed' });
              close();
              toast('success','Connection updated', saved.name+' saved.');
              reloadAll();
            } catch (err) { toast('error','Could not save', err.message); }
          }}],
        });
      }

      function showDetails(cn){
        openModal({ title: cn.name, icon:'plug', wide:true,
          body:`<div class="card-loading" style="height:220px"></div>`,
          footer:[
            {label:'Test Connection', cls:'primary', onClick:(close)=>{ close(); testConn(cn); }},
            {label:'Close'},
          ],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.connections.get(cn.id)
              .then(c => {
                body.innerHTML = `<div class="grid g2">
                  <div>${inspSection('Connection Details','info', kv([
                    ['Status', badge(c.status, null, true)],
                    ['Health', statusText(c.health, HEALTH_COLOR[c.health])],
                    ['Type', text(c.kind)],
                    ...(c.metadata_pairs||[]).map(m=>[m[0], esc(m[1])]),
                    ['Last Sync', rel(c.last_sync_at)],
                    ['Avg Latency', msecs(c.latency_ms)],
                    ['Syncs Today', num(c.syncs_today)],
                    ['Agents Using', num(c.linked_agent_count)],
                    ['Enabled', c.enabled ? '<span class="st-green">Yes</span>' : '<span class="st-red">No</span>'],
                    ...(c.note ? [['Note', `<span class="st-amber">${esc(c.note)}</span>`]] : []),
                  ]))}</div>
                  <div>${inspSection('Endpoint & Credentials','lock', kv([
                    ['Endpoint', (c.config||{}).endpoint_url ? `<span class="mono small">${esc(c.config.endpoint_url)}</span>` : dash],
                    ['Credential', c.credential_secret_id ? `<span class="link" data-nav="secrets">${esc(c.credential_secret_id)}</span>` : dash],
                    ['Created', `${day(c.created_at)}${c.created_by?' · '+esc(c.created_by):''}`],
                    ['Last Modified', `${when(c.updated_at)}${c.updated_by?' · '+esc(c.updated_by):''}`],
                  ]))}
                  ${inspSection('Configuration','settings', Object.keys(c.config||{}).length
                    ? `<div class="quote" style="font-family:Consolas,monospace;font-size:11px;white-space:pre-wrap">${esc(JSON.stringify(c.config, null, 2))}</div>`
                    : '<span class="faint">No configuration recorded.</span>')}</div>
                </div>`;
              })
              .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, null, 'this connection')); });
          },
        });
      }

      /* ---- header + quick actions ---- */
      function reloadAll(){ loadSummary(); loadGrid(); loadActivity(); }

      document.getElementById('ccSearch').addEventListener('input', e=>{
        query = e.target.value.trim();
        clearTimeout(searchTimer);
        searchTimer = setTimeout(loadGrid, 250);
      });
      document.getElementById('ccDonutMode').addEventListener('change', e=>{ donutMode = e.target.value; paintDonut(); });
      document.getElementById('ccRefresh').addEventListener('click', ()=>{ reloadAll(); toast('info','Refreshing','Re-reading every connection from the control plane.'); });

      document.getElementById('qaHealth').addEventListener('click', ()=>APP.go('metrics'));

      document.getElementById('qaTest').addEventListener('click', async (e)=>{
        if(!allowed('operator','Testing connections')) return;
        const btn = e.currentTarget, orig = btn.innerHTML;
        btn.disabled = true; btn.innerHTML = `<span class="spin">${ICONS.refresh}</span>Testing…`;
        try {
          const res = await Store.mutate(()=>API.connections.testAll(), { event:'connections:changed' });
          toast(res.ok ? 'success' : 'warn','All connections tested', res.message);
        } catch (err) { toast('error','Could not test the connections', err.message); }
        btn.disabled = false; btn.innerHTML = orig;
        reloadAll();
      });

      document.getElementById('qaSync').addEventListener('click', async (e)=>{
        if(!allowed('operator','Syncing connections')) return;
        const btn = e.currentTarget, orig = btn.innerHTML;
        btn.disabled = true; btn.innerHTML = `<span class="spin">${ICONS.refresh}</span>Syncing…`;
        try {
          const res = await Store.mutate(()=>API.connections.syncAll(), { event:'connections:changed' });
          toast(res.ok ? 'success' : 'warn','Sync complete', res.message);
        } catch (err) { toast('error','Could not sync', err.message); }
        btn.disabled = false; btn.innerHTML = orig;
        reloadAll();
      });

      document.getElementById('ccAdd').addEventListener('click', ()=>{
        if(!allowed('admin','Adding a connection')) return;
        openModal({
          title:'Add Connection', icon:'plus',
          body:`<div class="form-row"><label>CONNECTION TYPE</label><select class="filter-select w-100" id="ncType" style="height:34px">
              ${optionList(['Azure AI Foundry','Copilot Studio','MCP Server','Vector Database','Microsoft Purview','Custom REST API'])}</select></div>
            <div class="form-row"><label>DISPLAY NAME</label><input class="input" id="ncName" placeholder="e.g. Fulcrum-Foundry-EU"></div>
            <div class="form-row"><label>ENDPOINT / RESOURCE URI</label><input class="input" id="ncUri" placeholder="https://…"></div>
            <div class="form-row"><label>AUTHENTICATION</label><select class="filter-select w-100" id="ncAuth" style="height:34px">${optionList(AUTH_MODES)}</select></div>
            <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="ncEnv" style="height:34px">${optionList(ENVIRONMENTS, 'Production')}</select></div>`,
          footer:[{label:'Cancel'},{label:'Connect & Validate', cls:'primary', onClick: async (close, modal)=>{
            const kind = modal.querySelector('#ncType').value;
            const name = modal.querySelector('#ncName').value.trim() || (kind + ' — New');
            const endpoint = modal.querySelector('#ncUri').value.trim();
            if(endpoint && !/^https?:\/\//i.test(endpoint)){ toast('error','Check the endpoint','The endpoint must be an absolute http(s) URL.'); return; }
            const body = {
              name, kind, logo_key: KIND_LOGO[kind] || 'custom',
              metadata_pairs: [['Environment', modal.querySelector('#ncEnv').value], ['Authentication', modal.querySelector('#ncAuth').value]],
              config: { endpoint_url: endpoint || null },
              enabled: true,
            };
            try {
              const created = await Store.mutate(()=>API.connections.create(body), { event:'connections:changed' });
              close();
              toast('success','Connection added', `${created.name} registered — test it to confirm the endpoint answers.`);
              reloadAll();
            } catch (err) { toast('error','Could not add the connection', err.message); }
          }}],
        });
      });

      document.getElementById('ccActAll').addEventListener('click', ()=>openModal({
        title:'Connection Activity — Full Log', icon:'history', wide:true,
        body:`<div id="ccActFull"></div>`, footer:[{label:'Close'}],
        onOpen(modal){
          const host = modal.querySelector('#ccActFull');
          const at = dataTable({
            columns:[
              {key:'occurred_at', label:'Time', render:r=>`<span class="dim nowrap">${esc(relTime(ts(r.occurred_at)))}</span>`},
              {key:'connection_name', label:'Connection', sortable:false, render:r=>`<span class="flex" style="gap:7px"><span style="width:16px;height:16px;display:inline-flex">${logoFor(r.connection_logo_key)}</span><b style="font-size:12px">${esc(r.connection_name||'—')}</b></span>`},
              {key:'event', label:'Event', render:r=>esc(r.event)},
              {key:'status', label:'Status', render:r=>statusText(r.status, ACTIVITY_COLOR[r.status])},
              {key:'details', label:'Details', sortable:false, render:r=>dim(r.details)},
            ],
            rowId:'id', pageSize:10, itemName:'activity records', emptyText:'No connection activity recorded',
            searchPlaceholder:'Search activity…',
            defaultSort:{key:'occurred_at', dir:-1},
            filters:[{key:'status', label:'Status', param:'status', options:['Success','Warning','Error'], allLabel:'All Status'}],
            source:(params)=>API.connections.activity(params),
          });
          host.appendChild(at.filterEl);
          host.appendChild(at.el);
        },
      }));

      reloadAll();
    },
  };

  /* ============================== AGENT REGISTRY ============================== */

  SCREENS['agents'] = {
    title:'Agent Registry',
    render(main){
      let currentAgentId = null;

      main.innerHTML = `
        ${pageHead({title:'Agent Registry', sub:'View and manage all AI agents across your organization.',
          actions:`${searchBox('agSearch','Search agents…')}
          <button class="btn" id="agCols">${ICONS.columns}Columns</button>
          <button class="btn" id="agExport">${ICONS.download}Export</button>
          <button class="btn primary" id="agNew">${ICONS.plus}New Agent</button>`})}
        <div id="agKpis">${kpiSkeleton(['Total Agents','Active Agents','High Risk Agents','Policy Violations','Pending Approval','Inactive Agents'])}</div>
        <div class="with-inspector" id="agLayout">
          <div id="agTableWrap"></div>
          <div class="inspector" id="agInspector"></div>
        </div>`;

      requireRole(document.getElementById('agNew'), 'admin', 'Registering an agent');

      function loadSummary(){
        const host = document.getElementById('agKpis');
        if(!host) return;
        API.agents.summary()
          .then(s => {
            if(!document.getElementById('agKpis')) return;
            const violationDelta = s.policy_violations_30d - s.policy_violations_previous_30d;
            host.innerHTML = kpiRow([
              {label:'Total Agents', value:num(s.total), icon:'bot', color:'purple',
                delta: s.total_added_30d ? s.total_added_30d + ' vs last 30 days' : null,
                dir: deltaDir(s.total_added_30d), good:true, sub: s.total_added_30d ? null : 'No change in 30 days'},
              {label:'Active Agents', value:`<span class="st-green">${num(s.active)}</span>`,
                sub: pct(s.active_percent) + ' of total', icon:'checkCircle', color:'green'},
              {label:'High Risk Agents', value:num(s.high_risk), icon:'shield', color:'orange',
                delta: s.high_risk_added_30d ? s.high_risk_added_30d + ' vs last 30 days' : null,
                dir: deltaDir(s.high_risk_added_30d), good:false, sub: s.high_risk_added_30d ? null : 'No change in 30 days'},
              {label:'Policy Violations', value:num(s.policy_violations_30d), icon:'alert', color:'red',
                delta: violationDelta ? Math.abs(violationDelta) + ' vs previous 30 days' : null,
                dir: deltaDir(violationDelta), good: violationDelta <= 0,
                sub: violationDelta ? null : 'Unchanged vs previous 30 days'},
              {label:'Pending Approval', value:num(s.pending_approval), sub:'Awaiting governance review', icon:'clock', color:'amber'},
              {label:'Inactive Agents', value:num(s.inactive), sub: pct(s.inactive_percent) + ' of total', icon:'xCircle', color:'gray'},
            ], 190);
            fillFilter(table, 5, (s.owners||[]).map(o=>o.name));
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSummary, 'the agent summary')); });
      }

      /* ---- the ten columns, and the picker that hides them ---- */
      const ALL_COLUMNS = [
        { key:'name', label:'Agent Name', render:r=>entityCell(r.name,
            r.description ? (r.description.length > 46 ? r.description.slice(0,46)+'…' : r.description) : '',
            'bot', r.risk==='High'?'red':r.risk==='Medium'?'purple':'blue') },
        { key:'platform', label:'Source', render:r=>r.platform?platformCell(r.platform):dash },
        { key:'agent_type', label:'Type', render:r=>dim(r.agent_type) },
        { key:'environment', label:'Environment', render:r=>r.environment?badge(r.environment):dash },
        { key:'status', label:'Status', render:r=>statusText(r.status, r.status==='Active'?'green':r.status==='Inactive'?'gray':'purple') },
        { key:'risk', label:'Risk', render:r=>r.risk?riskBadge(r.risk):dash },
        { key:'policy_status', label:'Policy Status', render:r=>r.policy_status?badge(r.policy_status):dash },
        { key:'owner_name', label:'Owner', sortable:false, render:r=>r.owner_name?ownerCell(r.owner_name, r.team||''):dash },
        { key:'last_used_at', label:'Last Used', render:r=>`<span class="dim nowrap">${r.last_used_at?esc(relTime(ts(r.last_used_at))):'—'}</span>` },
      ];
      const visible = new Set(ALL_COLUMNS.map(c=>c.key));
      const columns = ALL_COLUMNS.slice();

      function applyColumns(){
        columns.length = 0;
        ALL_COLUMNS.forEach(c=>{ if(visible.has(c.key)) columns.push(c); });
        table.refresh();
      }

      const table = dataTable({
        columns, rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'agents', selectable:true,
        searchPlaceholder:'Search agents by name, owner, model…',
        defaultSort:{key:'last_used_at', dir:-1},
        emptyText:'No agents registered yet',
        filters:[
          {key:'source', label:'Source', param:'source', options:PLATFORMS, allLabel:'All Sources'},
          {key:'environment', label:'Environment', param:'environment', options:ENVIRONMENTS, allLabel:'All Environments'},
          {key:'status', label:'Status', param:'status', options:['Active','Inactive','Pending Review'], allLabel:'All Status'},
          {key:'risk_level', label:'Risk Level', param:'risk_level', options:RISKS, allLabel:'All Risk Levels'},
          {key:'policy_status', label:'Policy Status', param:'policy_status', options:POLICY_STATUSES, allLabel:'All Policy States'},
          {key:'owner', label:'Owner', param:'owner', options:[], allLabel:'All Owners'},
        ],
        source: (params) => API.agents.list(params),
        exportSource: (params) => API.agents.export(params),
        autoSelectFirst: true,
        onSelect: showAgent,
        rowActions: r=>[
          {label:'View Full Details', icon:'eye', onClick:()=>APP.go('agent/'+r.id)},
          {label:'View Live Runs', icon:'activity', onClick:()=>APP.go('live-runs')},
          {label:'View Metrics', icon:'chart', onClick:()=>{ APP.metricsAgent = r.id; APP.go('metrics'); }},
          {label:'Edit Agent', icon:'edit', onClick:()=>editAgent(r)},
          {label:'Clone Agent', icon:'copy', onClick:()=>cloneAgent(r)},
          {sep:true},
          r.status==='Active'
            ? {label:'Deactivate Agent', icon:'xCircle', danger:true, onClick:()=>deactivate(r)}
            : {label:'Activate Agent', icon:'checkCircle', onClick:()=>activate(r)},
        ],
      });

      const wrap = document.getElementById('agTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);

      document.getElementById('agSearch').addEventListener('input', e=>table.search(e.target.value));
      document.getElementById('agExport').addEventListener('click', ()=>table.export());
      document.getElementById('agCols').addEventListener('click', e=>C.openMenu(e.currentTarget,
        ALL_COLUMNS.map(c=>({ label: (visible.has(c.key) ? '✓ ' : '   ') + c.label, onClick: ()=>{
          if(visible.has(c.key)){
            if(visible.size === 1){ toast('warn','At least one column','A table needs a column to show.'); return; }
            visible.delete(c.key);
          } else visible.add(c.key);
          applyColumns();
        }}))));

      /* ---- mutations ---- */
      async function activate(r){
        if(!allowed('operator','Activating an agent')) return;
        try {
          const res = await Store.mutate(()=>API.agents.activate(r.id), { event:'agents:changed' });
          toast('success','Agent activated', res.message || `${r.name} is now active.`);
          table.refresh(); loadSummary();
          if(currentAgentId === r.id) showAgent(Object.assign({}, r, {status:'Active'}));
        } catch (err) { toast('error','Could not activate', err.message); }
      }

      function deactivate(r){
        if(!allowed('operator','Deactivating an agent')) return;
        confirmModal({ title:'Deactivate Agent', danger:true, confirmLabel:'Deactivate',
          body:`<p style="margin-top:0">You are about to deactivate <b style="color:var(--text)">${esc(r.name)}</b>.</p>
            <p>The agent stops accepting new runs immediately. In-flight executions complete. This is recorded in the audit trail.</p>
            <div class="form-row" style="margin-top:10px"><label>REASON (RECORDED)</label><input class="input" id="deactReason" placeholder="Why is this agent being deactivated?"></div>`,
          onConfirm: async (modal)=>{
            const reason = modal ? (modal.querySelector('#deactReason')||{}).value : '';
            try {
              const res = await Store.mutate(()=>API.agents.deactivate(r.id, reason ? { reason } : {}), { event:'agents:changed' });
              toast('success','Agent deactivated', res.message || `${r.name} is now inactive. Audit event recorded.`);
              table.refresh(); loadSummary();
              if(currentAgentId === r.id) showAgent(Object.assign({}, r, {status:'Inactive'}));
            } catch (err) { toast('error','Could not deactivate', err.message); }
          }});
      }

      function cloneAgent(r){
        if(!allowed('admin','Cloning an agent')) return;
        openModal({
          title:'Clone Agent — '+r.name, icon:'copy',
          body:`<div class="form-row"><label>NEW NAME</label><input class="input" id="clName" value="${esc(r.name)} (Copy)"></div>
            <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="clEnv" style="height:34px">${optionList(ENVIRONMENTS,'Development')}</select></div>
            <label class="flex" style="gap:8px;font-size:12.5px;margin-bottom:6px"><input type="checkbox" id="clConn" checked> Copy connector grants</label>
            <label class="flex" style="gap:8px;font-size:12.5px"><input type="checkbox" id="clPol" checked> Copy policy bindings</label>`,
          footer:[{label:'Cancel'},{label:'Clone Agent', cls:'primary', onClick: async (close, modal)=>{
            const body = { name: modal.querySelector('#clName').value.trim() || null,
              environment: modal.querySelector('#clEnv').value,
              copy_connectors: modal.querySelector('#clConn').checked,
              copy_policies: modal.querySelector('#clPol').checked };
            close();
            try {
              const created = await Store.mutate(()=>API.agents.clone(r.id, body), { event:'agents:changed' });
              toast('success','Agent cloned', `${created.name} created in ${created.environment}.`);
              table.refresh(); loadSummary();
            } catch (err) { toast('error','Could not clone', err.message); }
          }}],
        });
      }

      function editAgent(r){
        if(!allowed('admin','Editing an agent')) return;
        openModal({
          title:'Edit Agent — '+r.name, icon:'edit',
          body:`<div class="form-row"><label>AGENT NAME</label><input class="input" id="edName" value="${esc(r.name)}"></div>
            <div class="grid g2">
              <div class="form-row"><label>SOURCE PLATFORM</label><select class="filter-select w-100" id="edPlat" style="height:34px">${optionList(PLATFORMS, r.platform)}</select></div>
              <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="edEnv" style="height:34px">${optionList(ENVIRONMENTS, r.environment)}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>TYPE</label><select class="filter-select w-100" id="edType" style="height:34px">${optionList(AGENT_TYPES, r.agent_type)}</select></div>
              <div class="form-row"><label>RISK LEVEL</label><select class="filter-select w-100" id="edRisk" style="height:34px">${optionList(RISKS, r.risk)}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>MODEL</label><input class="input" id="edModel" value="${esc(r.model||'')}" placeholder="e.g. gpt-4o"></div>
              <div class="form-row"><label>TEAM</label><input class="input" id="edTeam" value="${esc(r.team||'')}"></div>
            </div>
            <div class="form-row"><label>OWNER</label><select class="filter-select w-100" id="edOwner" style="height:34px"><option value="">Loading members…</option></select></div>
            <div class="form-row"><label>DESCRIPTION</label><textarea class="input" id="edDesc" rows="2">${esc(r.description||'')}</textarea></div>`,
          footer:[{label:'Cancel'},{label:'Save Changes', cls:'primary', onClick: async (close, modal)=>{
            const body = {
              name: modal.querySelector('#edName').value.trim() || r.name,
              platform: modal.querySelector('#edPlat').value,
              environment: modal.querySelector('#edEnv').value,
              agent_type: modal.querySelector('#edType').value,
              risk: modal.querySelector('#edRisk').value,
              model: modal.querySelector('#edModel').value.trim() || null,
              team: modal.querySelector('#edTeam').value.trim() || null,
              description: modal.querySelector('#edDesc').value.trim() || null,
            };
            const owner = modal.querySelector('#edOwner').value;
            if(owner) body.owner_user_id = owner;
            close();
            try {
              const saved = await Store.mutate(()=>API.agents.update(r.id, body), { event:'agents:changed' });
              toast('success','Agent updated', `${saved.name} saved.`);
              table.refresh(); loadSummary();
              if(currentAgentId === r.id) showAgent(saved);
            } catch (err) { toast('error','Could not save the agent', err.message); }
          }}],
          onOpen(modal){ fillOwners(modal.querySelector('#edOwner'), r.owner_user_id); },
        });
      }

      function fillOwners(select, selectedId){
        if(!select) return;
        API.agents.owners({ page_size: 100, sort:'full_name' })
          .then(page => {
            const members = page.items || [];
            select.innerHTML = `<option value="">Unassigned</option>` + members.map(m=>
              `<option value="${esc(m.user_id || m.id)}" ${(m.user_id||m.id)===selectedId?'selected':''}>${esc(m.full_name || m.email)}${m.team?' — '+esc(m.team):''}</option>`).join('');
          })
          .catch(err => { select.innerHTML = `<option value="">Members unavailable — ${esc(err.message)}</option>`; });
      }

      document.getElementById('agNew').addEventListener('click', ()=>{
        if(!allowed('admin','Registering an agent')) return;
        openModal({
          title:'New Agent', icon:'bot',
          body:`<div class="form-row"><label>AGENT NAME</label><input class="input" id="naName" placeholder="e.g. Vendor Onboarding Agent"></div>
            <div class="grid g2">
              <div class="form-row"><label>SOURCE PLATFORM</label><select class="filter-select w-100" id="naPlat" style="height:34px">${optionList(PLATFORMS)}</select></div>
              <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="naEnv" style="height:34px">${optionList(ENVIRONMENTS,'Development')}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>TYPE</label><select class="filter-select w-100" id="naType" style="height:34px">${optionList(AGENT_TYPES)}</select></div>
              <div class="form-row"><label>RISK LEVEL</label><select class="filter-select w-100" id="naRisk" style="height:34px">${optionList(RISKS)}</select></div>
            </div>
            <div class="form-row"><label>DESCRIPTION</label><textarea class="input" id="naDesc" rows="2" placeholder="What does this agent do?"></textarea></div>
            <div class="form-row"><label>OWNER</label><select class="filter-select w-100" id="naOwner" style="height:34px"><option value="">Loading members…</option></select></div>`,
          footer:[{label:'Cancel'},{label:'Create Agent', cls:'primary', onClick: async (close, modal)=>{
            const body = {
              name: modal.querySelector('#naName').value.trim(),
              platform: modal.querySelector('#naPlat').value,
              environment: modal.querySelector('#naEnv').value,
              agent_type: modal.querySelector('#naType').value,
              risk: modal.querySelector('#naRisk').value,
              description: modal.querySelector('#naDesc').value.trim() || null,
            };
            const owner = modal.querySelector('#naOwner').value;
            if(owner) body.owner_user_id = owner;
            if(!body.name){ toast('error','Name required','An agent needs a name before it can be registered.'); return; }
            close();
            try {
              const created = await Store.mutate(()=>API.agents.create(body), { event:'agents:changed' });
              toast('success','Agent created', `${created.name} registered — status ${created.status}.`);
              table.refresh(); loadSummary();
            } catch (err) { toast('error','Could not create the agent', err.message); }
          }}],
          onOpen(modal){ fillOwners(modal.querySelector('#naOwner'), null); },
        });
      });

      /* ---- inspector: the detail payload, fetched when a row is chosen ---- */
      function showAgent(row){
        const insp = document.getElementById('agInspector');
        if(!insp || !row) return;
        currentAgentId = row.id;
        document.getElementById('agLayout').classList.remove('collapsed');
        insp.innerHTML = `
          <div class="insp-head">
            <span class="entity-ico" style="width:38px;height:38px;background:var(--purple-dim);color:var(--purple-bright)">${ICONS.bot}</span>
            <div class="grow"><div class="insp-title">${esc(row.name)}</div>
              <div class="flex" style="gap:6px;margin-top:4px">${row.platform?platformCell(row.platform):''}${row.environment?badge(row.environment):''}</div></div>
            <button class="icon-btn insp-close" id="agInspClose">${ICONS.x}</button></div>
          <div class="card-loading" style="height:240px;margin:12px"></div>`;
        insp.querySelector('#agInspClose').addEventListener('click', ()=>document.getElementById('agLayout').classList.add('collapsed'));

        API.agents.get(row.id)
          .then(detail => { if(currentAgentId === row.id) paintAgent(insp, detail); })
          .catch(err => {
            if(currentAgentId !== row.id) return;
            const holder = insp.querySelector('.card-loading');
            if(holder) holder.replaceWith(screenError(err, ()=>showAgent(row), 'this agent'));
          });
      }

      function paintAgent(insp, detail){
        const a = detail.agent || {}, m = a.metrics || {}, stats = detail.stats || {};
        insp.innerHTML = `
          <div class="insp-head">
            <span class="entity-ico" style="width:38px;height:38px;background:var(--purple-dim);color:var(--purple-bright)">${ICONS.bot}</span>
            <div class="grow"><div class="insp-title">${esc(a.name)}</div>
              <div class="flex" style="gap:6px;margin-top:4px">${a.platform?platformCell(a.platform):''}${a.environment?badge(a.environment):''}</div></div>
            <button class="icon-btn insp-close" id="agInspClose">${ICONS.x}</button></div>
          <div class="flex" style="gap:8px;margin:12px 0 2px">
            <button class="btn sm grow" style="justify-content:center" data-nav="agent/${esc(a.id)}">View Details</button>
            <button class="btn sm grow" style="justify-content:center" id="agInspEdit">Edit Agent</button></div>
          ${inspSection('Overview','info', kv([
            ['Status', statusText(a.status, a.status==='Active'?'green':a.status==='Inactive'?'gray':'purple')],
            ['Type', text(a.agent_type)],
            ['Owner', a.owner_name ? esc(a.owner_name) + (a.team?' ('+esc(a.team)+')':'') : dash],
            ['Created On', day(a.created_at)],
            ['Last Modified', day(a.updated_at)],
            ['Description', a.description ? `<span class="dim" style="font-size:11.5px">${esc(a.description)}</span>` : dash],
          ]) + ((a.tags||[]).length ? `<div style="margin-top:8px">${a.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>` : ''))}
          ${inspSection('Configuration','settings', kv([
            ['Model', text(a.model)],
            ['Prompt Version', text(a.prompt_version)],
            ['Tools', `${num(a.tools_enabled)} enabled · ${detail.connectors.length} granted`],
            ['Policies', `${num(a.policies_applied)} applied · ${detail.policies.length} bound`],
            ['Memory Policy', text(a.memory_policy)],
            ['Retry Policy', a.retries == null ? dash : a.retries + ' retries'],
            ['Access Scope', text(a.access_scope)],
          ]))}
          ${inspSection('Status','gauge', kv([
            ['Risk Level', a.risk?riskBadge(a.risk):dash],
            ['Policy Status', a.policy_status?badge(a.policy_status):dash],
            ['Last Used', rel(a.last_used_at)],
            ['Total Runs (30d)', num(m.runs_30d)],
            ['Success Rate (30d)', pct(m.success_rate_30d)],
            ['Eval Score', m.eval_score == null ? dash : Number(m.eval_score).toFixed(2)],
            ['Telemetry', detail.telemetry_available ? '<span class="st-green">Reporting</span>' : '<span class="faint">No telemetry yet</span>'],
            ['Runs In Window', num(stats.run_count)],
          ]))}
          <div class="insp-section"><button class="link" data-nav="agent/${esc(a.id)}">View Full Agent Details ${ICONS.arrowRight}</button></div>`;
        insp.querySelector('#agInspClose').addEventListener('click', ()=>document.getElementById('agLayout').classList.add('collapsed'));
        requireRole(insp.querySelector('#agInspEdit'), 'admin', 'Editing an agent')
          .addEventListener('click', ()=>editAgent(a));
      }

      loadSummary();
    },
  };

  /* ================================ AGENT DETAIL ================================
   *
   * One GET /agents/{id} carries identity, configuration, connector grants,
   * policy bindings, prompt versions and the run counters the engine reported,
   * so the nine tabs open without nine round trips. Tabs that need a paged
   * resource of their own (runs, audit) ask for it when they are opened.
   */

  SCREENS['agent'] = {
    title:'Agent Detail',
    render(main, param){
      const agentId = param;
      let detail = null, tabsEl = null, activeTab = 0;

      if(!agentId){
        main.innerHTML = pageHead({title:'Agent Detail', sub:'No agent was named in the link.'});
        main.appendChild(screenError({ message:'Open an agent from the registry to see its detail.' },
          ()=>APP.go('agents'), 'this agent'));
        return;
      }

      main.innerHTML = `
        <div class="crumbs"><a data-nav="agents">Agent Registry</a><span class="sep">›</span><span class="cur">Loading…</span></div>
        <div id="adHead"><div class="card card-loading" style="height:120px"></div></div>
        <div id="adStrip"></div>
        <div id="adTabs"></div>
        <div id="adBody"><div class="card card-loading" style="height:260px"></div></div>`;

      load();

      function load(){
        const head = document.getElementById('adHead');
        const body = document.getElementById('adBody');
        if(!head) return;
        head.innerHTML = `<div class="card card-loading" style="height:120px"></div>`;
        body.innerHTML = `<div class="card card-loading" style="height:260px"></div>`;
        API.agents.get(agentId)
          .then(d => { if(document.getElementById('adHead')) paint(d); })
          .catch(err => {
            if(!document.getElementById('adHead')) return;
            head.innerHTML = '';
            head.appendChild(screenError(err, load, 'this agent'));
            body.innerHTML = '';
            const strip = document.getElementById('adStrip'); if(strip) strip.innerHTML = '';
            const tabs = document.getElementById('adTabs'); if(tabs) tabs.innerHTML = '';
          });
      }

      /* ---- header, strip and tab bar ---- */
      function paint(d){
        detail = d;
        const a = d.agent || {};
        const m = a.metrics || {};
        const crumb = main.querySelector('.crumbs .cur');
        if(crumb) crumb.textContent = a.name || agentId;
        document.title = (a.name || 'Agent Detail') + ' — Fulcrum Ops';

        document.getElementById('adHead').innerHTML = `
          <div class="page-head">
            <div>
              <h1 class="page-title" style="display:flex;align-items:center;gap:10px">${esc(a.name)}
                ${statusText(a.status, a.status==='Active'?'green':a.status==='Inactive'?'gray':'purple')}</h1>
              <div class="flex" style="gap:8px;margin-top:7px;flex-wrap:wrap">
                ${a.platform?platformCell(a.platform):''}
                ${a.agent_type?`<span class="badge bg-gray">${esc(a.agent_type)}</span>`:''}
                ${a.environment?badge(a.environment):''}
                <span class="dim">Owner: <b style="color:var(--text)">${esc(a.owner_name || 'Unassigned')}</b>${a.team?' ('+esc(a.team)+')':''}</span>
              </div>
              <div class="page-sub" style="margin-top:7px">${a.description?esc(a.description):'<span class="faint">No description recorded.</span>'}</div>
            </div>
            <div class="page-actions">
              <button class="btn orange" id="adRun">${ICONS.play}Run Agent</button>
              <button class="btn" data-nav="live-runs">${ICONS.activity}View Live Runs</button>
              <button class="btn" id="adMore">More Actions ${ICONS.chevDown}</button>
            </div>
          </div>`;

        document.getElementById('adStrip').innerHTML = `
          <div class="card" style="padding:11px 16px;margin-bottom:16px">
            <div class="flex flex-wrap" style="gap:26px">
              <div><div class="small faint" style="font-weight:700">RISK LEVEL</div><div style="margin-top:3px">${a.risk?riskBadge(a.risk):dash}</div></div>
              <div><div class="small faint" style="font-weight:700">POLICY STATUS</div><div style="margin-top:3px">${a.policy_status?badge(a.policy_status):dash}</div></div>
              <div><div class="small faint" style="font-weight:700">LAST USED</div><div style="margin-top:3px;font-weight:700">${rel(a.last_used_at)}</div></div>
              <div><div class="small faint" style="font-weight:700">TOTAL RUNS (30D)</div><div style="margin-top:3px;font-weight:700">${num(m.runs_30d)}</div></div>
              <div><div class="small faint" style="font-weight:700">EVAL SCORE</div><div style="margin-top:3px;font-weight:700">${m.eval_score == null ? dash : Number(m.eval_score).toFixed(2)}</div></div>
              <div><div class="small faint" style="font-weight:700">TELEMETRY</div><div style="margin-top:3px;font-weight:700">${
                d.telemetry_available ? '<span class="st-green">Reporting</span>' : '<span class="faint">Not provisioned</span>'}</div></div>
            </div>
          </div>
          ${d.telemetry_available ? '' : `<div class="scan-note">${ICONS.info} This agent has no telemetry project yet, so run counters, prompt versions and latency are not available. Everything below is registry state.</div>`}`;

        requireRole(document.getElementById('adRun'), 'operator', 'Running an agent');
        document.getElementById('adRun').addEventListener('click', runAgent);
        document.getElementById('adMore').addEventListener('click', e=>C.openMenu(e.currentTarget, moreActions()));

        const host = document.getElementById('adTabs');
        host.innerHTML = '';
        tabsEl = tabBar(host, TABS.map(t=>({label:t})), i=>{ activeTab = i; renderTab(i); }, activeTab);
        renderTab(activeTab);
      }

      const TABS = ['Overview','Runs','Model & Prompt','Tools & Connectors','Security & Access',
                    'Memory & State','Evaluations','Configuration','Audit Trail'];

      /* ---- header actions ---- */
      function moreActions(){
        const a = detail.agent;
        return [
          {label:'Edit Agent', icon:'edit', onClick:()=>editAgent()},
          {label:'Clone Agent', icon:'copy', onClick:()=>cloneAgent()},
          {label:'Export Configuration', icon:'download', onClick:()=>exportConfiguration()},
          {sep:true},
          a.status==='Active'
            ? {label:'Deactivate Agent', icon:'xCircle', danger:true, onClick:()=>deactivate()}
            : {label:'Activate Agent', icon:'checkCircle', onClick:()=>activate()},
        ];
      }

      async function runAgent(){
        if(!allowed('operator','Running an agent')) return;
        openModal({
          title:'Run '+detail.agent.name, icon:'play',
          body:`<div class="form-row"><label>INPUT</label><input class="input" id="adRunInput" placeholder="What should the agent be asked?"></div>
            <p class="small muted" style="margin:0">The run opens immediately on Live Runs and is completed by the agent's runtime through the ingest API.</p>`,
          footer:[{label:'Cancel'},{label:'Start Run', cls:'orange', onClick: async (close, modal)=>{
            const input = modal.querySelector('#adRunInput').value.trim();
            close();
            try {
              const started = await Store.mutate(()=>API.agents.run(detail.agent.id, input ? { input } : {}),
                { event:'runs:changed' });
              const data = started.data || {};
              toast('success','Run started', `${esc(String(data.run_id || started.entity_id || '').slice(0,14))}… — follow it on Live Runs.`);
              load();
            } catch (err) { toast('error','Could not start the run', err.message); }
          }}],
        });
      }

      async function activate(){
        if(!allowed('operator','Activating an agent')) return;
        try {
          const res = await Store.mutate(()=>API.agents.activate(detail.agent.id), { event:'agents:changed' });
          toast('success','Agent activated', res.message || `${detail.agent.name} is now active.`);
          load();
        } catch (err) { toast('error','Could not activate', err.message); }
      }

      function deactivate(){
        if(!allowed('operator','Deactivating an agent')) return;
        confirmModal({ title:'Deactivate Agent', danger:true, confirmLabel:'Deactivate',
          body:`<p style="margin-top:0">You are about to deactivate <b style="color:var(--text)">${esc(detail.agent.name)}</b>.</p>
            <p>New runs are rejected immediately; in-flight executions finish. The change is recorded in the audit trail.</p>
            <div class="form-row" style="margin-top:10px"><label>REASON (RECORDED)</label><input class="input" id="adDeactReason" placeholder="Why is this agent being deactivated?"></div>`,
          onConfirm: async (modal)=>{
            const reason = modal ? (modal.querySelector('#adDeactReason')||{}).value : '';
            try {
              const res = await Store.mutate(()=>API.agents.deactivate(detail.agent.id, reason ? { reason } : {}), { event:'agents:changed' });
              toast('success','Agent deactivated', res.message || `${detail.agent.name} is now inactive.`);
              load();
            } catch (err) { toast('error','Could not deactivate', err.message); }
          }});
      }

      function cloneAgent(){
        if(!allowed('admin','Cloning an agent')) return;
        const a = detail.agent;
        openModal({
          title:'Clone Agent — '+a.name, icon:'copy',
          body:`<div class="form-row"><label>NEW NAME</label><input class="input" id="adClName" value="${esc(a.name)} (Copy)"></div>
            <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="adClEnv" style="height:34px">${optionList(ENVIRONMENTS,'Development')}</select></div>
            <label class="flex" style="gap:8px;font-size:12.5px;margin-bottom:6px"><input type="checkbox" id="adClConn" checked> Copy connector grants</label>
            <label class="flex" style="gap:8px;font-size:12.5px"><input type="checkbox" id="adClPol" checked> Copy policy bindings</label>`,
          footer:[{label:'Cancel'},{label:'Clone Agent', cls:'primary', onClick: async (close, modal)=>{
            const body = { name: modal.querySelector('#adClName').value.trim() || null,
              environment: modal.querySelector('#adClEnv').value,
              copy_connectors: modal.querySelector('#adClConn').checked,
              copy_policies: modal.querySelector('#adClPol').checked };
            close();
            try {
              const created = await Store.mutate(()=>API.agents.clone(a.id, body), { event:'agents:changed' });
              toast('success','Agent cloned', `${created.name} created in ${created.environment}.`);
              APP.go('agent/'+created.id);
            } catch (err) { toast('error','Could not clone', err.message); }
          }}],
        });
      }

      function editAgent(){
        if(!allowed('admin','Editing an agent')) return;
        const a = detail.agent;
        openModal({
          title:'Edit Agent — '+a.name, icon:'edit',
          body:`<div class="form-row"><label>AGENT NAME</label><input class="input" id="adEdName" value="${esc(a.name)}"></div>
            <div class="grid g2">
              <div class="form-row"><label>SOURCE PLATFORM</label><select class="filter-select w-100" id="adEdPlat" style="height:34px">${optionList(PLATFORMS, a.platform)}</select></div>
              <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="adEdEnv" style="height:34px">${optionList(ENVIRONMENTS, a.environment)}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>TYPE</label><select class="filter-select w-100" id="adEdType" style="height:34px">${optionList(AGENT_TYPES, a.agent_type)}</select></div>
              <div class="form-row"><label>RISK LEVEL</label><select class="filter-select w-100" id="adEdRisk" style="height:34px">${optionList(RISKS, a.risk)}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>MODEL</label><input class="input" id="adEdModel" value="${esc(a.model||'')}" placeholder="e.g. gpt-4o"></div>
              <div class="form-row"><label>TEAM</label><input class="input" id="adEdTeam" value="${esc(a.team||'')}"></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>MEMORY POLICY</label><input class="input" id="adEdMem" value="${esc(a.memory_policy||'')}" placeholder="e.g. Conversation 90d"></div>
              <div class="form-row"><label>ACCESS SCOPE</label><input class="input" id="adEdScope" value="${esc(a.access_scope||'')}" placeholder="e.g. Finance read-only"></div>
            </div>
            <div class="form-row"><label>DESCRIPTION</label><textarea class="input" id="adEdDesc" rows="2">${esc(a.description||'')}</textarea></div>`,
          footer:[{label:'Cancel'},{label:'Save Changes', cls:'primary', onClick: async (close, modal)=>{
            const body = {
              name: modal.querySelector('#adEdName').value.trim() || a.name,
              platform: modal.querySelector('#adEdPlat').value,
              environment: modal.querySelector('#adEdEnv').value,
              agent_type: modal.querySelector('#adEdType').value,
              risk: modal.querySelector('#adEdRisk').value,
              model: modal.querySelector('#adEdModel').value.trim() || null,
              team: modal.querySelector('#adEdTeam').value.trim() || null,
              memory_policy: modal.querySelector('#adEdMem').value.trim() || null,
              access_scope: modal.querySelector('#adEdScope').value.trim() || null,
              description: modal.querySelector('#adEdDesc').value.trim() || null,
              expected_updated_at: a.updated_at,
            };
            close();
            try {
              const saved = await Store.mutate(()=>API.agents.update(a.id, body), { event:'agents:changed' });
              toast('success','Agent updated', `${saved.name} saved.`);
              load();
            } catch (err) { toast('error','Could not save the agent', err.message); }
          }}],
        });
      }

      /** The manifest the server exports, written out as a two-column file. */
      async function exportConfiguration(){
        try {
          const cfg = await API.agents.configuration(detail.agent.id);
          const rows = Object.keys(cfg).map(k=>({ k, v: Array.isArray(cfg[k]) ? cfg[k].join(' | ')
            : (cfg[k] && typeof cfg[k] === 'object' ? JSON.stringify(cfg[k]) : cfg[k]) }));
          U.downloadCSV(String(cfg.name || 'agent').replace(/\s+/g,'-').toLowerCase()+'-configuration',
            [{label:'Field', key:'k'},{label:'Value', key:'v'}], rows);
          toast('success','Configuration exported', `${cfg.name} manifest downloaded.`);
        } catch (err) {
          toast('error','Could not export the configuration', err.message);
        }
      }

      /* ---- shared run columns ---- */
      const runCols = [
        {key:'id', label:'Run ID', render:r=>`<span class="mono">${esc(String(r.id).slice(0,10))}…</span>`},
        {key:'status', label:'Status', render:r=>statusText(r.status)},
        {key:'occurred_at', label:'Time', render:r=>`<span class="dim nowrap">${rel(r.occurred_at)}</span>`},
        {key:'model', label:'Model', render:r=>dim(r.model)},
        {key:'duration_seconds', label:'Duration', align:'right', cls:'num', render:r=>secs(r.duration_seconds)},
        {key:'tokens', label:'Tokens', align:'right', cls:'num', render:r=>num(r.tokens)},
        {key:'cost', label:'Cost', align:'right', cls:'num', render:r=>r.cost==null?dash:fmtMoney(r.cost,3)},
        {key:'policy', label:'Policy', render:r=>r.policy?badge(r.policy):dash},
        {key:'confidence', label:'Confidence', align:'right', cls:'num', render:r=>r.confidence==null?dash:Number(r.confidence).toFixed(2)},
      ];

      function runActions(r){
        return [
          {label:'Open in Replay Studio', icon:'replay', onClick:()=>{ APP.replayRun = r.id; APP.go('replay'); }},
          {label:'View in Live Runs', icon:'activity', onClick:()=>APP.go('live-runs')},
        ];
      }

      /* ================================ tabs ================================ */
      function renderTab(i){
        const body = document.getElementById('adBody');
        if(!body || !detail) return;
        const t = TABS[i];
        if(t === 'Overview') tabOverview(body);
        else if(t === 'Runs') tabRuns(body);
        else if(t === 'Model & Prompt') tabPrompt(body);
        else if(t === 'Tools & Connectors') tabConnectors(body);
        else if(t === 'Security & Access') tabSecurity(body);
        else if(t === 'Memory & State') tabMemory(body);
        else if(t === 'Evaluations') tabEvaluations(body);
        else if(t === 'Configuration') tabConfiguration(body);
        else tabAudit(body);
      }

      /* ---- 1. Overview ---- */
      function tabOverview(body){
        const a = detail.agent, m = a.metrics || {};
        const checks = [
          ['Telemetry project provisioned', detail.telemetry_available],
          ['No policy violations in 30 days', (m.violations_30d || 0) === 0],
          [`${detail.policies.length} ${detail.policies.length === 1 ? 'policy' : 'policies'} bound`, detail.policies.length > 0],
          [`${detail.connectors.filter(c=>c.is_blocked).length} blocked connector grants`, detail.connectors.every(c=>!c.is_blocked)],
          ['Approved by governance review', a.status === 'Active'],
        ];
        body.innerHTML = `
          <div class="two-col">
            <div style="display:flex;flex-direction:column;gap:14px">
              <div class="grid g2">
                <div class="card"><div class="card-head"><div class="card-title">Agent Health</div>
                  <span class="small faint">${a.last_used_at ? 'Last run ' + esc(rel(a.last_used_at)) : 'No runs yet'}</span></div>
                  ${a.health == null
                    ? EMPTY('gauge','No health score yet','A composite score appears once the agent has reported runs.')
                    : `<div class="donut-wrap">${gaugeRing(a.health, a.health>=80?'green':a.health>=60?'amber':'red', 118,
                        a.health>=80?'Healthy':a.health>=60?'Degraded':'Unhealthy')}
                      <div class="grow" style="display:flex;flex-direction:column;gap:7px;font-size:12px">
                        ${checks.map(c=>`<span class="flex" style="gap:8px">${c[1]
                          ? `<span style="color:#15803D;width:14px;display:inline-flex">${ICONS.checkCircle}</span>`
                          : `<span style="color:#B45309;width:14px;display:inline-flex">${ICONS.alert}</span>`}
                          <span class="${c[1]?'':'st-amber'}">${esc(c[0])}</span></span>`).join('')}
                      </div></div>`}</div>
                <div class="card"><div class="card-head"><div class="card-title">Recent Activity <span class="muted">(Last 30 Days)</span></div></div>
                  <div class="grid g2" style="gap:9px">
                    ${[['Runs', num(m.runs_30d)], ['Success Rate', pct(m.success_rate_30d)],
                       ['Avg Latency', secs(m.avg_latency_seconds)], ['Policy Violations', num(m.violations_30d)],
                       ['Tokens', num(m.tokens_30d)], ['Cost', m.cost_30d == null ? dash : fmtMoney(m.cost_30d, 2)]].map(x=>
                      `<div style="background:var(--panel-2);border:1px solid var(--border-soft);border-radius:9px;padding:9px 11px">
                        <div class="small muted">${x[0]}</div><div style="font-size:17px;font-weight:700;margin-top:2px">${x[1]}</div></div>`).join('')}
                  </div>
                  <div class="faint small" style="margin-top:8px">${m.computed_at
                    ? 'Computed ' + esc(rel(m.computed_at)) + ' from the telemetry engine.'
                    : 'These counters have not been computed yet.'}</div></div>
              </div>
              <div class="grid g2">
                <div class="card">${inspSection('Agent Information','info', kv([
                  ['Agent ID', `<span class="mono">${esc(a.id)}</span>`],
                  ['Source', text(a.platform)], ['Type', text(a.agent_type)],
                  ['Environment', a.environment?badge(a.environment):dash],
                  ['Created On', day(a.created_at)],
                  ['Owner', a.owner_name ? esc(a.owner_name) + (a.team?' ('+esc(a.team)+')':'') : dash],
                  ['Owner Email', text(a.owner_email)],
                  ['Primary Model', text(a.model)],
                  ['Prompt Version', text(a.prompt_version)],
                  ['Telemetry Project', text(a.engine_project_name)],
                  ['Last Modified', `${day(a.updated_at)}${a.updated_by?' · '+esc(a.updated_by):''}`],
                ]))}
                <div style="margin-top:8px">${(a.tags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join('') || '<span class="faint small">No tags</span>'}</div></div>
                <div class="card">${inspSection('Risk & Policy Summary','shield', kv([
                  ['Risk Level', a.risk?riskBadge(a.risk):dash],
                  ['Policy Status', a.policy_status?badge(a.policy_status):dash],
                  ['Policies Bound', String(detail.policies.length)],
                  ['Policies Applied', num(a.policies_applied)],
                  ['Violations (30d)', num(m.violations_30d)],
                  ['Escalations (30d)', num(m.escalations_30d)],
                  ['Human Review', a.policy_status === 'Approval Required' ? '<span class="st-amber">Required</span>' : 'Not required'],
                ]))}
                ${detail.policies.length ? `<div style="margin-top:6px">${detail.policies.slice(0,4).map(p=>
                  `<div class="kv"><span class="k">${esc(p.name)}</span><span class="v">${badge(p.enforcement, ENF_COLORS[p.enforcement]||'gray')}</span></div>`).join('')}</div>` : ''}
                <button class="link" data-nav="policies" style="margin-top:6px">View Policy Details ${ICONS.arrowRight}</button></div>
              </div>
              <div class="card pad-0" style="padding:16px 16px 0">
                <div class="card-head" style="margin-bottom:6px"><div class="card-title">Recent Runs</div>
                  <button class="link" data-nav="live-runs">View All Runs ${ICONS.arrowRight}</button></div>
                <div id="adRecentRuns"></div>
              </div>
            </div>
            <div style="display:flex;flex-direction:column;gap:14px">
              <div class="card"><div class="card-head"><div class="card-title">Model & Prompt</div></div>
                ${kv([['Model', text(a.model)], ['Prompt Version', text(a.prompt_version)],
                      ['Versions Committed', String(detail.versions.length)],
                      ['Current Commit', currentVersion() ? `<span class="mono small">${esc(String(currentVersion().commit||'').slice(0,10) || '—')}</span>` : dash]])}
                <button class="link" id="adGoPrompt" style="margin-top:6px">Open the Model & Prompt tab ${ICONS.arrowRight}</button></div>
              <div class="card"><div class="card-head"><div class="card-title">Tools & Connectors</div></div>
                ${kv([['Tools Enabled', num(a.tools_enabled)],
                      ['Connector Grants', String(detail.connectors.length)],
                      ['External', String(detail.connectors.filter(c=>c.data_classification && c.data_classification !== 'Internal').length)],
                      ['Blocked', String(detail.connectors.filter(c=>c.is_blocked).length)]])}
                <div class="flex flex-wrap" style="gap:5px;margin-top:8px">${
                  detail.connectors.slice(0,8).map(c=>`<span class="tag">${esc(c.name)}</span>`).join('')
                  || '<span class="faint small">No connector grants yet</span>'}</div>
                <button class="link" data-nav="connectors" style="margin-top:8px">View Tools & Connectors ${ICONS.arrowRight}</button></div>
              <div class="card" id="adEvalCard"><div class="card-head"><div class="card-title">Evaluations</div></div>
                <div class="card-loading" style="height:110px"></div></div>
              <div class="card"><div class="card-head"><div class="card-title">Quick Actions</div></div>
                <div style="display:flex;flex-direction:column;gap:8px">
                  <button class="btn block" id="adQaEdit">${ICONS.edit}Edit Agent</button>
                  <button class="btn block" id="adQaClone">${ICONS.copy}Clone Agent</button>
                  <button class="btn block" id="adQaExport">${ICONS.download}Export Configuration</button>
                  <button class="btn block ghost-danger" id="adQaDeact">${ICONS.xCircle}${a.status==='Active'?'Deactivate Agent':'Activate Agent'}</button>
                </div></div>
            </div>
          </div>`;

        const rt = dataTable({
          columns: runCols, rowId:'id', pageSize:5, pageSizes:[5,10], itemName:'runs',
          emptyText: detail.telemetry_available ? 'No runs reported in this window' : 'No telemetry project, so no runs',
          defaultSort:{key:'occurred_at', dir:-1},
          extraParams:{ agent_id: a.id },
          source:(params)=>API.agents.runs(a.id, params),
          rowActions: runActions,
        });
        document.getElementById('adRecentRuns').appendChild(rt.el);

        requireRole(document.getElementById('adQaEdit'), 'admin', 'Editing an agent')
          .addEventListener('click', editAgent);
        requireRole(document.getElementById('adQaClone'), 'admin', 'Cloning an agent')
          .addEventListener('click', cloneAgent);
        document.getElementById('adQaExport').addEventListener('click', exportConfiguration);
        requireRole(document.getElementById('adQaDeact'), 'operator', 'Changing an agent status')
          .addEventListener('click', ()=>{ if(a.status==='Active') deactivate(); else activate(); });
        document.getElementById('adGoPrompt').addEventListener('click', ()=>selectTab(2));

        loadEvalCard();
      }

      function selectTab(i){
        activeTab = i;
        if(tabsEl) tabsEl.querySelectorAll('.tab').forEach((t,ti)=>t.classList.toggle('active', ti===i));
        renderTab(i);
      }

      function currentVersion(){
        return (detail.versions || []).find(v=>v.is_current) || (detail.versions || [])[0] || null;
      }

      function loadEvalCard(){
        const host = document.getElementById('adEvalCard');
        if(!host) return;
        API.evaluations.list({ agent_id: detail.agent.id, page_size: 1, sort: '-occurred_at' })
          .then(page => {
            if(!document.getElementById('adEvalCard')) return;
            const ev = (page.items || [])[0];
            host.innerHTML = `<div class="card-head"><div class="card-title">Evaluations</div>
                ${ev?`<span class="small faint">${esc(rel(ev.occurred_at))}</span>`:''}</div>` + (ev
              ? `<div class="donut-wrap">${gaugeRing((ev.avg_score == null ? 0 : ev.avg_score) * 100, 'purple', 104, 'Avg Score')}
                  <div class="legend grow">
                    ${[['Correctness',ev.correctness],['Grounding',ev.grounding],['Faithfulness',ev.faithfulness],['Safety',ev.safety]].map(x=>
                      `<div class="legend-item"><span class="sw" style="background:${U.cc(x[1]==null?'gray':x[1]>=0.9?'green':'amber')}"></span>
                        <span class="lg-label">${x[0]}</span><span class="lg-val">${x[1]==null?'—':Number(x[1]).toFixed(2)}</span></div>`).join('')}
                  </div></div>
                <button class="link" id="adEvalMore" style="margin-top:4px">View evaluation detail ${ICONS.arrowRight}</button>`
              : EMPTY('beaker','No evaluation runs yet','Run one from the Evaluations tab to score this agent.'));
            const more = document.getElementById('adEvalMore');
            if(more) more.addEventListener('click', ()=>selectTab(6));
          })
          .catch(err => {
            if(!document.getElementById('adEvalCard')) return;
            host.innerHTML = `<div class="card-head"><div class="card-title">Evaluations</div></div>`;
            host.appendChild(screenError(err, loadEvalCard, 'the evaluation summary'));
          });
      }

      /* ---- 2. Runs ---- */
      function tabRuns(body){
        const a = detail.agent;
        body.innerHTML = `<div id="adRunsTbl"></div>`;
        const rt = dataTable({
          columns: runCols, rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'runs',
          searchPlaceholder:'Search this agent’s runs…',
          defaultSort:{key:'occurred_at', dir:-1},
          emptyText: detail.telemetry_available ? 'No runs in this window' : 'No telemetry project, so no runs',
          filters:[
            {key:'status', label:'Status', param:'status', options:['Completed','Warned','Failed','Running'], allLabel:'All Status'},
            {key:'policy', label:'Policy', param:'policy', options:['Allowed','Warned','Blocked'], allLabel:'All'},
            {key:'risk', label:'Risk', param:'risk', options:RISKS, allLabel:'All Risk Levels'},
            {key:'time_range', label:'Time Range', param:'time_range', options:['Last hour','Last 6 hours','Last 24 hours'], allLabel:'Last 24 hours'},
          ],
          extraParams:{ agent_id: a.id },
          source:(params)=>API.agents.runs(a.id, params),
          exportSource:(params)=>API.runs.export(Object.assign({ agent_id: a.id }, params)),
          rowActions: runActions,
        });
        const w = document.getElementById('adRunsTbl');
        w.appendChild(rt.filterEl);
        w.appendChild(rt.el);
      }

      /* ---- 3. Model & Prompt ---- */
      function tabPrompt(body){
        const a = detail.agent, versions = detail.versions || [], cur = currentVersion();
        body.innerHTML = `<div class="two-col">
          <div class="card"><div class="card-head"><div class="card-title">System Prompt ${cur?badge(cur.status):''}</div>
            <span class="small faint">${cur?esc(cur.version):esc(a.prompt_version||'—')}${cur && cur.token_count!=null?' · '+fmtFull(cur.token_count)+' tokens':''}</span></div>
            ${cur && cur.template_preview
              ? `<div class="quote" style="font-size:12.5px;line-height:1.7;white-space:pre-wrap">${esc(cur.template_preview)}</div>`
              : EMPTY('pen','No prompt committed','Commit a version to keep this agent’s system prompt under review.')}
            <div class="flex" style="gap:8px;margin-top:10px">
              <button class="btn sm" data-nav="prompts">${ICONS.edit}Open in Prompt Manager</button>
              <button class="btn sm" id="adDiff" ${versions.length < 2 ? 'disabled title="Two committed versions are needed to compare"' : ''}>${ICONS.git}Compare Versions</button>
              <button class="btn sm" id="adNewVersion">${ICONS.plus}Create New Version</button></div></div>
          <div style="display:flex;flex-direction:column;gap:14px">
            <div class="card"><div class="card-head"><div class="card-title">Model Configuration</div></div>
              ${kv([['Primary Model', text(a.model)],
                    ['Prompt Version', text(a.prompt_version)],
                    ['Retries', a.retries == null ? dash : String(a.retries)],
                    ['Tools Enabled', num(a.tools_enabled)],
                    ['Memory Policy', text(a.memory_policy)],
                    ['Access Scope', text(a.access_scope)],
                    ['Telemetry Project', text(a.engine_project_name)]])}
              <div class="faint small" style="margin-top:8px">Sampling parameters are held by the runtime, not the registry.</div></div>
            <div class="card"><div class="card-head"><div class="card-title">Version History</div>
              <span class="small faint">${versions.length} ${versions.length===1?'version':'versions'}</span></div>
              ${versions.length ? `<div class="pipe">${versions.map((v,vi)=>`
                <div class="pipe-step"><div class="pipe-dot ${v.is_current?'active':'done'}">${v.is_current?ICONS.star:ICONS.check}</div>
                <div class="pipe-body"><div class="pipe-title"><span>${esc(v.version)} — ${esc(v.status)}</span><span class="faint">${esc(rel(v.created_at))}</span></div>
                <div class="pipe-sub">${esc(v.change_description || 'No description recorded')}${v.author?' · by '+esc(v.author):''}</div></div></div>`).join('')}</div>`
                : EMPTY('history','No version history', detail.telemetry_available
                    ? 'Commit a prompt version and it appears here.'
                    : 'Versions live with the telemetry project, which this agent does not have yet.')}</div>
          </div></div>`;

        const diffBtn = document.getElementById('adDiff');
        if(diffBtn && !diffBtn.disabled) diffBtn.addEventListener('click', openDiff);
        requireRole(document.getElementById('adNewVersion'), 'admin', 'Committing a prompt version')
          .addEventListener('click', newVersion);
      }

      function openDiff(){
        const versions = detail.versions || [];
        const opts = (sel) => versions.map(v=>`<option value="${esc(v.commit || v.version)}" ${
          (v.commit || v.version) === sel ? 'selected' : ''}>${esc(v.version)}${v.is_current?' (current)':''}</option>`).join('');
        const older = versions[versions.length - 1], newer = versions[0];
        openModal({
          title:'Version Comparison', icon:'git', wide:true,
          body:`<div class="flex" style="gap:10px;align-items:flex-end;margin-bottom:12px">
              <div class="form-row" style="margin:0"><label>FROM</label><select class="filter-select" id="adDiffFrom" style="height:32px">${opts(older.commit || older.version)}</select></div>
              <div class="form-row" style="margin:0"><label>TO</label><select class="filter-select" id="adDiffTo" style="height:32px">${opts(newer.commit || newer.version)}</select></div>
              <button class="btn sm" id="adDiffGo">${ICONS.refresh}Compare</button>
            </div>
            <div id="adDiffOut"><div class="card-loading" style="height:180px"></div></div>`,
          footer:[{label:'Close'}],
          onOpen(modal){
            const out = modal.querySelector('#adDiffOut');
            function run(){
              const from = modal.querySelector('#adDiffFrom').value;
              const to = modal.querySelector('#adDiffTo').value;
              out.innerHTML = '<div class="card-loading" style="height:180px"></div>';
              API.agents.versionDiff(detail.agent.id, { from, to })
                .then(d => {
                  out.innerHTML = `<div class="flex" style="gap:14px;margin-bottom:10px;flex-wrap:wrap">
                      ${kv([['From', esc(d.from_version.version)]])}${kv([['To', esc(d.to_version.version)]])}
                      ${kv([['Added', `<span class="st-green">+${d.added_lines}</span>`]])}
                      ${kv([['Removed', `<span class="st-red">-${d.removed_lines}</span>`]])}
                    </div>
                    ${d.identical ? EMPTY('checkCircle','The two versions are identical','Nothing changed between these commits.')
                      : `<div class="quote" style="font-family:Consolas,monospace;font-size:11.5px;white-space:pre-wrap">${
                        d.diff.map(l=>{
                          const color = l.op === '+' ? '#15803D' : l.op === '-' ? '#B91C1C' : l.op === '@' ? 'var(--purple-bright)' : 'inherit';
                          return `<span style="color:${color}">${esc(l.op + ' ' + l.text)}</span>`;
                        }).join('\n')}</div>`}
                    ${(d.metadata_changes||[]).length ? inspSection('Metadata Changes','settings', kv(
                      d.metadata_changes.map(mc=>[mc.key, `${esc(JSON.stringify(mc.before))} → <b>${esc(JSON.stringify(mc.after))}</b>`]))) : ''}`;
                })
                .catch(err => { out.innerHTML = ''; out.appendChild(screenError(err, run, 'the version comparison')); });
            }
            modal.querySelector('#adDiffGo').addEventListener('click', run);
            run();
          },
        });
      }

      function newVersion(){
        if(!allowed('admin','Committing a prompt version')) return;
        const cur = currentVersion();
        openModal({
          title:'Create New Version', icon:'git', wide:true,
          body:`<div class="form-row"><label>SYSTEM PROMPT</label>
              <textarea class="input" id="adVerText" rows="10" placeholder="The full system prompt for this agent…">${esc(cur && cur.template_preview ? cur.template_preview : '')}</textarea></div>
            <div class="form-row"><label>WHAT CHANGED</label><input class="input" id="adVerNote" placeholder="e.g. Tightened the discrepancy threshold to 2%"></div>
            <label class="flex" style="gap:8px;font-size:12.5px"><input type="checkbox" id="adVerCurrent" checked> Make this the agent’s current version</label>`,
          footer:[{label:'Cancel'},{label:'Commit Version', cls:'primary', onClick: async (close, modal)=>{
            const template = modal.querySelector('#adVerText').value;
            if(!template.trim()){ toast('error','Prompt required','A version needs a prompt body before it can be committed.'); return; }
            const body = { template, change_description: modal.querySelector('#adVerNote').value.trim() || null,
              make_current: modal.querySelector('#adVerCurrent').checked };
            close();
            try {
              const v = await Store.mutate(()=>API.agents.createVersion(detail.agent.id, body), { event:'agents:changed' });
              toast('success','Version committed', `${v.version} recorded${v.is_current?' and set as current':''}.`);
              load();
            } catch (err) { toast('error','Could not commit the version', err.message); }
          }}],
        });
      }

      /* ---- 4. Tools & Connectors ---- */
      function tabConnectors(body){
        body.innerHTML = `<div id="adConnTbl"></div>`;
        // These rows are the grants carried on the detail payload; there is no
        // per-agent connector collection to page through.
        const ct = dataTable({
          rows: detail.connectors, rowId:'grant_id', pageSize:10, itemName:'granted tools',
          exportName:(detail.agent.name||'agent').replace(/\s+/g,'-').toLowerCase()+'-connectors',
          emptyText:'This agent has no connector grants',
          columns:[
            {key:'name', label:'Connector / Tool', render:r=>entityCell(r.name, r.provider || '', 'link', 'purple')},
            {key:'connector_type', label:'Type', render:r=>badge(r.connector_type, r.connector_type==='MCP Server'?'purple':r.connector_type==='Tool'?'cyan':r.connector_type==='Data Source'?'blue':'gray')},
            {key:'provider', label:'Provider', render:r=>dim(r.provider)},
            {key:'risk_level', label:'Risk', render:r=>r.risk_level?riskBadge(r.risk_level):dash},
            {key:'access', label:'Access', render:r=>r.access?badge(r.access, r.access==='Admin'?'red':r.access==='Read-Write'?'amber':'green'):dash},
            {key:'data_classification', label:'Classification', render:r=>r.data_classification?badge(r.data_classification):dash},
            {key:'scopes', label:'Scopes', sortable:false, render:r=>(r.scopes||[]).length
              ? `<span class="dim small">${esc(r.scopes.slice(0,3).join(', '))}${r.scopes.length>3?' +'+(r.scopes.length-3):''}</span>` : dash},
            {key:'status', label:'Status', render:r=>statusText(r.status)},
            {key:'granted_at', label:'Granted', render:r=>`<span class="dim nowrap">${rel(r.granted_at)}</span>`},
            {key:'last_used_at', label:'Last Used', render:r=>`<span class="dim nowrap">${rel(r.last_used_at)}</span>`},
          ],
          rowActions: r=>[
            {label:'View in Governance', icon:'external', onClick:()=>APP.go('connectors')},
            {label:'Test Connection', icon:'activity', onClick:()=>testConnector(r)},
          ],
        });
        document.getElementById('adConnTbl').appendChild(ct.el);
      }

      async function testConnector(r){
        if(!allowed('member','Testing a connector')) return;
        toast('info','Testing '+r.name+'…','Probing the registered endpoint.');
        try {
          const res = await API.connectors.test(r.connector_id);
          toast(res.ok ? 'success' : 'warn', `${res.name}: ${res.status}`,
            res.message + (res.latency_ms != null ? ` (${res.latency_ms} ms)` : ''));
        } catch (err) { toast('error','Test failed', err.message); }
      }

      /* ---- 5. Security & Access ---- */
      function tabSecurity(body){
        const a = detail.agent;
        const grants = detail.connectors || [];
        const allowedGrants = grants.filter(c=>!c.is_blocked);
        const deniedGrants = grants.filter(c=>c.is_blocked);
        const classes = Array.from(new Set(grants.map(c=>c.data_classification).filter(Boolean)));
        const protectionPolicies = (detail.policies||[]).filter(p=>p.category === 'Data Protection' || p.category === 'Security');
        body.innerHTML = `<div class="grid g2">
          <div class="card">${inspSection('Access & Identity','lock', kv([
            ['Access Scope', text(a.access_scope)],
            ['Environment', a.environment?badge(a.environment):dash],
            ['Telemetry Project', text(a.engine_project_name)],
            ['Owner', a.owner_name ? esc(a.owner_name) : dash],
            ['Owner Email', text(a.owner_email)],
            ['Registered By', text(a.created_by)],
            ['Last Modified By', text(a.updated_by)],
            ['Authentication Modes', Array.from(new Set(grants.map(c=>c.auth_mode).filter(Boolean))).map(x=>esc(x)).join(', ') || dash],
          ]))}</div>
          <div class="card">${inspSection('Data Protection','shieldCheck', kv([
            ['Classifications In Reach', classes.length ? classes.map(c=>badge(c)).join(' ') : dash],
            ['External Connectors', String(grants.filter(c=>c.data_classification && c.data_classification !== 'Internal').length)],
            ['Memory Policy', text(a.memory_policy)],
            ['Policy Status', a.policy_status?badge(a.policy_status):dash],
            ['Data Protection Policies', protectionPolicies.length ? protectionPolicies.map(p=>esc(p.name)).join('<br>') : dash],
          ]))}</div>
          <div class="card"><div class="card-head"><div class="card-title">Permitted Actions</div></div>
            ${allowedGrants.length ? allowedGrants.map(c=>
              `<div class="flex" style="gap:9px;padding:5px 0"><span style="color:#15803D;width:14px;display:inline-flex">${ICONS.check}</span>
                <span class="dim" style="font-size:12.5px">${esc(c.access || 'Read')} on <b style="color:var(--text)">${esc(c.name)}</b>${
                  (c.scopes||[]).length ? ' — ' + esc(c.scopes.join(', ')) : ''}</span></div>`).join('')
              : '<div class="faint small" style="padding:6px 0">No connector grants, so this agent may call nothing outside its model.</div>'}
            ${deniedGrants.map(c=>
              `<div class="flex" style="gap:9px;padding:5px 0"><span style="color:#B91C1C;width:14px;display:inline-flex">${ICONS.x}</span>
                <span class="dim" style="font-size:12.5px"><b style="color:var(--text)">${esc(c.name)}</b> — blocked for every agent</span></div>`).join('')}
          </div>
          <div class="card"><div class="card-head"><div class="card-title">Recent Access Events</div>
            <button class="link" id="adSecMore">Full audit trail ${ICONS.arrowRight}</button></div>
            <div id="adSecEvents"><div class="card-loading" style="height:120px"></div></div></div>
        </div>`;
        document.getElementById('adSecMore').addEventListener('click', ()=>selectTab(8));
        loadSecurityEvents();
      }

      function loadSecurityEvents(){
        const host = document.getElementById('adSecEvents');
        if(!host) return;
        API.audit.list({ entity_id: detail.agent.id, page_size: 6, sort: '-occurred_at' })
          .then(page => {
            if(!document.getElementById('adSecEvents')) return;
            const rows = page.items || [];
            host.innerHTML = rows.length
              ? rows.map(e=>`<div class="kv"><span class="k">${esc(e.action)}</span>
                  <span class="v small">${esc(e.detail || e.entity_label || '—')} · <span class="faint">${esc(rel(e.occurred_at))}</span></span></div>`).join('')
              : EMPTY('history','No recorded events','Changes to this agent appear here as they happen.');
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSecurityEvents, 'the access events')); });
      }

      /* ---- 6. Memory & State ---- */
      function tabMemory(body){
        const a = detail.agent;
        body.innerHTML = `<div class="grid g2">
          <div class="card">${inspSection('Memory Policy','brain', kv([
            ['Policy', text(a.memory_policy)],
            ['Environment', a.environment?badge(a.environment):dash],
            ['Retry Policy', a.retries == null ? dash : a.retries + ' retries · exponential backoff'],
            ['Access Scope', text(a.access_scope)],
            ['Telemetry Project', text(a.engine_project_name)],
          ]))}
          <button class="link" data-nav="memory" style="margin-top:6px">Open Memory & State Management ${ICONS.arrowRight}</button></div>
          <div class="card"><div class="card-head"><div class="card-title">Memory Stores in ${esc(a.environment || 'this environment')}</div></div>
            <div id="adMemStores"><div class="card-loading" style="height:130px"></div></div></div>
        </div>`;
        loadMemoryStores();
      }

      function loadMemoryStores(){
        const host = document.getElementById('adMemStores');
        if(!host) return;
        API.memory.list({ environment: detail.agent.environment, page_size: 6, sort:'name' })
          .then(page => {
            if(!document.getElementById('adMemStores')) return;
            const rows = page.items || [];
            host.innerHTML = rows.length
              ? rows.map(s=>`<div class="kv"><span class="k">${esc(s.name)}</span>
                  <span class="v">${U.barPct(s.usage_percent || 0, (s.usage_percent||0) > 80 ? 'amber' : 'green')}</span></div>`).join('')
                + `<div class="divider"></div>` + kv([
                    ['Stores In Environment', String(page.total)],
                    ['Records Held', fmtFull(rows.reduce((n,s)=>n + (s.record_count||0), 0))],
                    ['Active Sessions', fmtFull(rows.reduce((n,s)=>n + (s.active_session_count||0), 0))],
                  ])
              : EMPTY('brain','No memory stores in this environment','Register one on the Memory & State screen.');
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadMemoryStores, 'the memory stores')); });
      }

      /* ---- 7. Evaluations ---- */
      function tabEvaluations(body){
        body.innerHTML = `<div class="grid g2">
          <div class="card" id="adEvLatest"><div class="card-loading" style="height:260px"></div></div>
          <div class="card" id="adEvSettings"><div class="card-loading" style="height:200px"></div></div>
        </div>`;
        loadEvaluations();
      }

      function loadEvaluations(){
        const latest = document.getElementById('adEvLatest');
        const settings = document.getElementById('adEvSettings');
        if(!latest) return;
        Promise.all([
          API.evaluations.list({ agent_id: detail.agent.id, page_size: 1, sort: '-occurred_at' }),
          API.evaluations.trend({ agent_id: detail.agent.id, window_days: 30 }).catch(()=>({ points: [] })),
        ])
          .then(([page, trend]) => {
            if(!document.getElementById('adEvLatest')) return;
            const ev = (page.items || [])[0];
            const points = (trend.points || []).filter(p=>p.avg_score != null);
            latest.innerHTML = ev
              ? `<div class="card-head"><div class="card-title">Latest Evaluation — ${esc(ev.dataset)}</div>
                  <span class="small faint">${esc(rel(ev.occurred_at))} · ${fmtFull(ev.cases)} cases</span></div>
                <div class="grid g4" style="gap:9px">
                  ${[['Correctness',ev.correctness],['Grounding',ev.grounding],['Faithfulness',ev.faithfulness],['Safety',ev.safety]].map(x=>
                    `<div style="background:var(--panel-2);border:1px solid var(--border-soft);border-radius:9px;padding:10px;text-align:center">
                      <div class="small muted">${x[0]}</div>
                      <div style="font-size:20px;font-weight:700" class="${x[1]==null?'':x[1]>=0.9?'sc-good':x[1]>=0.8?'sc-mid':'sc-bad'}">${x[1]==null?'—':Number(x[1]).toFixed(2)}</div></div>`).join('')}
                </div>
                <div class="mt">${points.length > 1
                  ? U.lineChart({ series:[{color:'purple', points:points.map(p=>p.avg_score), area:true, dots:true}],
                      h:170, yFmt:v=>v.toFixed(2), xLabels:points.map(p=>String(p.date).slice(5)) })
                  : EMPTY('chart','Not enough history to chart','A trend appears once this agent has more than one scored run.')}</div>
                ${ev.is_regression ? `<div class="scan-note">${ICONS.alert} This run regressed against its baseline by ${Math.abs(ev.baseline_delta || 0).toFixed(3)}.</div>` : ''}`
              : `<div class="card-head"><div class="card-title">Latest Evaluation</div></div>` +
                EMPTY('beaker','No evaluation runs for this agent','Start one with Run Evaluation Now.');

            settings.innerHTML = `<div class="card-head"><div class="card-title">Evaluation Settings</div></div>
              ${kv([
                ['Last Dataset', ev ? esc(ev.dataset) : dash],
                ['Judge Model', ev ? esc(ev.judge_model) : dash],
                ['Cases', ev ? fmtFull(ev.cases) : dash],
                ['Average Score', ev && ev.avg_score != null ? Number(ev.avg_score).toFixed(3) : dash],
                ['Baseline', ev && ev.baseline_avg_score != null ? Number(ev.baseline_avg_score).toFixed(3) : dash],
                ['Delta vs Baseline', ev && ev.baseline_delta != null
                  ? `<span class="${ev.baseline_delta < 0 ? 'st-red' : 'st-green'}">${ev.baseline_delta > 0 ? '+' : ''}${Number(ev.baseline_delta).toFixed(3)}</span>` : dash],
                ['Runs In Window', String((trend.points || []).reduce((n,p)=>n + (p.evaluations||0), 0))],
                ['Owner', detail.agent.owner_name ? esc(detail.agent.owner_name) : dash],
              ])}
              <button class="btn sm mt" id="adEvRun">${ICONS.play}Run Evaluation Now</button>
              <button class="link" data-nav="evaluations" style="margin-top:8px;display:block">Open the Evaluations screen ${ICONS.arrowRight}</button>`;
            requireRole(document.getElementById('adEvRun'), 'operator', 'Starting an evaluation')
              .addEventListener('click', runEvaluation);
          })
          .catch(err => {
            if(!document.getElementById('adEvLatest')) return;
            latest.innerHTML = ''; latest.appendChild(screenError(err, loadEvaluations, 'the evaluations'));
            settings.innerHTML = '';
          });
      }

      function runEvaluation(){
        if(!allowed('operator','Starting an evaluation')) return;
        openModal({
          title:'Run Evaluation — '+detail.agent.name, icon:'beaker',
          body:`<div class="form-row"><label>DATASET</label><select class="filter-select w-100" id="adEvDataset" style="height:34px"><option value="">Loading datasets…</option></select></div>
            <div class="form-row"><label>JUDGE MODEL</label><input class="input" id="adEvJudge" value="gpt-4o"></div>
            <div class="form-row"><label>NOTES</label><input class="input" id="adEvNotes" placeholder="Why is this run being made?"></div>`,
          footer:[{label:'Cancel'},{label:'Run Evaluation', cls:'primary', onClick: async (close, modal)=>{
            const dataset = modal.querySelector('#adEvDataset').value;
            if(!dataset){ toast('error','Dataset required','Choose a dataset for the run.'); return; }
            const body = { agent_id: detail.agent.id, dataset,
              judge_model: modal.querySelector('#adEvJudge').value.trim() || 'gpt-4o',
              notes: modal.querySelector('#adEvNotes').value.trim() || null };
            close();
            try {
              const started = await Store.mutate(()=>API.evaluations.run(body), { event:'evaluations:changed' });
              toast('success','Evaluation started', `${started.name || dataset} — status ${started.status}.`);
              loadEvaluations();
            } catch (err) { toast('error','Could not start the evaluation', err.message); }
          }}],
          onOpen(modal){
            const select = modal.querySelector('#adEvDataset');
            API.evaluations.datasets({ page_size: 50 })
              .then(page => {
                const items = page.items || [];
                select.innerHTML = items.length
                  ? items.map(d=>`<option value="${esc(d.name)}">${esc(d.name)}${d.item_count!=null?' — '+d.item_count+' cases':''}</option>`).join('')
                  : '<option value="">No datasets registered yet</option>';
              })
              .catch(err => { select.innerHTML = `<option value="">Datasets unavailable — ${esc(err.message)}</option>`; });
          },
        });
      }

      /* ---- 8. Configuration ---- */
      function tabConfiguration(body){
        const cfg = detail.configuration || {};
        body.innerHTML = `<div class="two-col">
          <div class="card"><div class="card-head"><div class="card-title">Configuration Manifest</div>
            <div class="flex" style="gap:8px">
              <button class="btn sm" id="adCfgCopy">${ICONS.copy}Copy JSON</button>
              <button class="btn sm" id="adCfgExport">${ICONS.download}Export</button></div></div>
            <div class="quote" style="font-family:Consolas,monospace;font-size:11.5px;white-space:pre">${esc(JSON.stringify(cfg, null, 2))}</div></div>
          <div class="card"><div class="card-head"><div class="card-title">Linked Configuration</div></div>
            ${kv([
              ['Prompt Version', text(cfg.prompt_version)],
              ['Model', text(cfg.model)],
              ['Allowed Tools', (cfg.allowed_tools||[]).length ? esc(cfg.allowed_tools.join(', ')) : dash],
              ['Memory Policy', text(cfg.memory_policy)],
              ['Retry Policy', cfg.retry_policy ? `${cfg.retry_policy.max_retries} retries · ${esc(cfg.retry_policy.backoff)}` : dash],
              ['Access Scope', text(cfg.access_scope)],
              ['Telemetry Project', text(cfg.telemetry_project)],
              ['Exported At', when(cfg.exported_at)],
            ])}
            ${(detail.policies||[]).length ? `<div class="divider"></div>${
              detail.policies.map(p=>`<div class="kv"><span class="k">${esc(p.category)}</span>
                <span class="v"><span class="link" data-nav="policies">${esc(p.name)}</span> ${badge(p.status)}</span></div>`).join('')}` : ''}
            <div class="divider"></div>
            <div class="flex" style="gap:8px">
              <button class="btn sm" id="adCfgVersion">${ICONS.git}Create New Version</button>
              <button class="btn sm" id="adCfgClone">${ICONS.copy}Clone</button></div></div>
        </div>`;
        document.getElementById('adCfgCopy').addEventListener('click', ()=>{
          const text_ = JSON.stringify(cfg, null, 2);
          if(navigator.clipboard && navigator.clipboard.writeText){
            navigator.clipboard.writeText(text_)
              .then(()=>toast('success','Copied to clipboard','The configuration manifest is on your clipboard.'))
              .catch(err=>toast('error','Could not copy', err.message || 'The browser refused clipboard access.'));
          } else {
            toast('error','Could not copy','This browser does not expose the clipboard to the page.');
          }
        });
        document.getElementById('adCfgExport').addEventListener('click', exportConfiguration);
        requireRole(document.getElementById('adCfgVersion'), 'admin', 'Committing a prompt version')
          .addEventListener('click', newVersion);
        requireRole(document.getElementById('adCfgClone'), 'admin', 'Cloning an agent')
          .addEventListener('click', cloneAgent);
      }

      /* ---- 9. Audit Trail ---- */
      function tabAudit(body){
        body.innerHTML = `<div id="adAuditTbl"></div>`;
        const at = dataTable({
          columns:[
            {key:'occurred_at', label:'Time', render:r=>`<span class="dim nowrap">${rel(r.occurred_at)}</span>`},
            {key:'actor', label:'Actor', sortable:false, render:r=>ownerCell(r.actor, '')},
            {key:'action', label:'Action', render:r=>`<span class="cell-main">${esc(r.action)}</span>`},
            {key:'entity_label', label:'Resource', sortable:false, render:r=>text(r.entity_label)},
            {key:'detail', label:'Detail', sortable:false, render:r=>dim(r.detail)},
            {key:'source_screen', label:'Source Screen', render:r=>r.source_screen?`<span class="badge bg-gray">${esc(r.source_screen)}</span>`:dash},
            {key:'ip_address', label:'IP', sortable:false, render:r=>r.ip_address?`<span class="mono">${esc(r.ip_address)}</span>`:dash},
          ],
          rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'audit events',
          searchPlaceholder:'Search this agent’s audit trail…',
          defaultSort:{key:'occurred_at', dir:-1},
          emptyText:'No audit events for this agent yet',
          filters:[{key:'action', label:'Action', param:'action', options:[], allLabel:'All Actions'}],
          extraParams:{ entity_id: detail.agent.id },
          source:(params)=>API.audit.list(params),
          exportSource:(params)=>API.audit.export(params),
          onLoad:(rows)=>fillFilter(at, 0, Array.from(new Set(rows.map(r=>r.action)))),
        });
        const w = document.getElementById('adAuditTbl');
        w.appendChild(at.filterEl);
        w.appendChild(at.el);
      }
    },
  };

  /* ===================== CONNECTOR & MCP GOVERNANCE ===================== */

  SCREENS['connectors'] = {
    title:'Connector & MCP Governance',
    render(main){
      let currentId = null;
      // Mutated by the tab bar and read on every request the table makes.
      const scope = {};

      main.innerHTML = `
        ${pageHead({title:'Connector & MCP Governance', sub:'Govern and monitor all tools, connectors, and MCP servers used by agents.',
          actions:`${searchBox('cnSearch','Search connectors, tools, MCP servers…')}
          <button class="btn" id="cnExport">${ICONS.download}Export</button>
          <button class="btn primary" id="cnAdd">${ICONS.plus}Add Connector</button>`})}
        <div id="cnTabs"></div>
        <div id="cnKpis">${kpiSkeleton(['Total Connectors','Active Connectors','External Connectors','High Risk Tools','Blocked Tools'])}</div>
        <div class="with-inspector" id="cnLayout">
          <div id="cnTableWrap"></div>
          <div class="inspector" id="cnInspector"></div>
        </div>`;

      requireRole(document.getElementById('cnAdd'), 'operator', 'Registering a connector');

      /* ---- KPI cards and the tab-bar counts, both off /connectors/summary ---- */
      let tabsEl = null;
      function loadSummary(){
        const host = document.getElementById('cnKpis');
        if(!host) return;
        API.connectors.summary()
          .then(s => {
            if(!document.getElementById('cnKpis')) return;
            host.innerHTML = kpiRow([
              {label:'Total Connectors', value:num(s.total), sub:`${num(s.mcp_servers)} MCP · ${num(s.tools)} tools · ${num(s.data_sources)} data sources`, icon:'box', color:'purple'},
              {label:'Active Connectors', value:`<span class="st-green">${num(s.active)}</span>`, sub:pct(s.active_percent)+' of total', icon:'checkCircle', color:'green'},
              {label:'External Connectors', value:num(s.external), sub:pct(s.external_percent)+' of total', icon:'globe', color:'orange'},
              {label:'High Risk Tools', value:num(s.high_risk), sub:s.high_risk?'Review the grants on these':'None registered', icon:'shield', color:'amber'},
              {label:'Blocked Tools', value:`<span class="st-red">${num(s.blocked)}</span>`, sub:s.blocked?'Agents fail closed on these':'None blocked', icon:'xCircle', color:'red'},
            ]);
            paintTabs(s);
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSummary, 'the connector summary')); });
      }

      const TAB_SCOPES = [
        {label:'All Connectors', apply:()=>{ delete scope.type; delete scope.access; }},
        {label:'MCP Servers', key:'mcp_servers', apply:()=>{ scope.type = 'MCP Server'; delete scope.access; }},
        {label:'Tools', key:'tools', apply:()=>{ scope.type = 'Tool'; delete scope.access; }},
        {label:'External Systems', key:'external', apply:()=>{ delete scope.type; scope.access = 'External'; }},
        {label:'Data Sources', key:'data_sources', apply:()=>{ scope.type = 'Data Source'; delete scope.access; }},
      ];
      let activeTab = 0;

      function paintTabs(s){
        const host = document.getElementById('cnTabs');
        if(!host) return;
        host.innerHTML = '';
        tabsEl = tabBar(host, TAB_SCOPES.map(t=>({label:t.label, count:t.key ? (s ? s[t.key] : null) : null})),
          i=>{ activeTab = i; TAB_SCOPES[i].apply(); syncScopedFilters(); table.state.page = 1; table.refresh(); }, activeTab);
      }
      paintTabs(null);

      /* extraParams overwrites same-named query params, so while a tab pins
         type or access the matching dropdown is parked rather than silently ignored. */
      function syncScopedFilters(){
        [['type','0'],['access','3']].forEach(([key, fi])=>{
          const sel = table.filterEl && table.filterEl.querySelector(`[data-fi="${fi}"]`);
          if(!sel) return;
          const pinned = scope[key] != null;
          sel.disabled = pinned;
          sel.title = pinned ? `The ${TAB_SCOPES[activeTab].label} tab is already filtering by ${key}.` : '';
          if(pinned && sel.value){ sel.value = ''; table.state.filters[key] = ''; }
        });
      }

      /* ---- the governance table ---- */
      const table = dataTable({
        columns:[
          {key:'name', label:'Connector / Tool', render:r=>entityCell(r.name, r.endpoint_url || r.provider || '',
            r.connector_type==='MCP Server'?'mcp':r.connector_type==='Data Source'?'database':'tool',
            r.risk_level==='High'?'red':r.risk_level==='Medium'?'amber':'blue')},
          {key:'connector_type', label:'Type', render:r=>badge(r.connector_type, r.connector_type==='MCP Server'?'purple':r.connector_type==='Tool'?'cyan':r.connector_type==='Data Source'?'blue':'gray')},
          {key:'provider', label:'Provider / System', render:r=>dim(r.provider)},
          {key:'used_by_agents', label:'Used By Agents', align:'center', cls:'num', render:r=>`<b>${num(r.used_by_agents)}</b>`},
          {key:'risk_level', label:'Risk Level', render:r=>r.risk_level?riskBadge(r.risk_level):dash},
          {key:'status', label:'Status', render:r=>statusText(r.status)},
          {key:'access', label:'Access', render:r=>r.access?badge(r.access, r.access==='Admin'?'red':r.access==='Read-Write'?'amber':'green'):dash},
          {key:'data_classification', label:'Classification', render:r=>r.data_classification?badge(r.data_classification):dash},
          {key:'last_used_at', label:'Last Used', render:r=>`<span class="dim nowrap">${rel(r.last_used_at)}</span>`},
        ],
        rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'connectors',
        searchPlaceholder:'Search by name, provider, type or endpoint…',
        defaultSort:{key:'name', dir:1},
        emptyText:'No connectors registered yet',
        filters:[
          {key:'type', label:'Type', param:'type', options:CONNECTOR_TYPES, allLabel:'All Types'},
          {key:'status', label:'Status', param:'status', options:CONNECTOR_STATUSES, allLabel:'All Status'},
          {key:'risk', label:'Risk Level', param:'risk', options:RISKS, allLabel:'All Risk Levels'},
          {key:'access', label:'Access', param:'access', options:['Internal','External','Read','Read-Write','Admin'], allLabel:'All Access Types'},
        ],
        extraParams: scope,
        source:(params)=>API.connectors.list(params),
        exportSource:(params)=>API.connectors.export(params),
        onSelect: showConn,
        // Rows arrive after construction and again on every tab change, so the
        // first row is chosen here rather than by an immediate selectFirst().
        onLoad:(rows)=>{
          if(!rows.length){
            currentId = null;
            const insp = document.getElementById('cnInspector');
            if(insp) insp.innerHTML = EMPTY('box','Nothing selected','Register a connector, or clear the filters, to inspect one here.');
            return;
          }
          if(!currentId || !rows.some(r=>r.id === currentId)) table.selectFirst();
        },
        rowActions: r=>[
          {label:'View Details', icon:'eye', onClick:()=>showConn(r)},
          {label:'Test Connection', icon:'activity', onClick:()=>testConn(r)},
          {label:'Edit Permissions', icon:'lock', onClick:()=>editPermissions(r)},
          // A blocked connector fails closed, so a fresh grant would be dead on arrival.
          ...(r.status !== 'Blocked' ? [{label:'Grant to Agent…', icon:'bot', onClick:()=>grantToAgent(r)}] : []),
          {sep:true},
          r.status !== 'Blocked'
            ? {label:'Block Connector', icon:'xCircle', danger:true, onClick:()=>blockConn(r)}
            : {label:'Unblock Connector', icon:'checkCircle', onClick:()=>unblockConn(r)},
        ],
      });

      const wrap = document.getElementById('cnTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);

      document.getElementById('cnSearch').addEventListener('input', e=>table.search(e.target.value));
      document.getElementById('cnExport').addEventListener('click', ()=>table.export());

      function refreshAll(){ table.refresh(); loadSummary(); }

      /* ---- mutations ---- */
      async function testConn(r){
        if(!allowed('member','Testing a connector')) return;
        toast('info','Testing '+r.name+'…','Probing the registered endpoint.');
        try {
          const res = await API.connectors.test(r.id);
          toast(res.ok ? 'success' : 'warn', `${res.name}: ${res.status}`,
            res.message + (res.latency_ms != null ? ` (${res.latency_ms} ms)` : ''));
          table.refresh();
          if(currentId === r.id) showConn(r);
        } catch (err) { toast('error','Test failed', err.message); }
      }

      function blockConn(r){
        if(!allowed('admin','Blocking a connector')) return;
        openModal({
          title:'Block Connector — '+r.name, icon:'xCircle',
          body:`<p style="margin-top:0">Every agent holding a grant on <b style="color:var(--text)">${esc(r.name)}</b> fails closed the moment this takes effect${
            r.used_by_agents ? ` — that is <b style="color:var(--text)">${r.used_by_agents}</b> agent(s)` : ''}.</p>
            <div class="form-row"><label>REASON (REQUIRED, RECORDED)</label>
              <textarea class="input" id="cnBlockReason" rows="3" placeholder="Why is this connector being blocked?"></textarea></div>`,
          footer:[{label:'Cancel'},{label:'Block Connector', cls:'danger', onClick: async (close, modal)=>{
            const reason = modal.querySelector('#cnBlockReason').value.trim();
            if(reason.length < 3){ toast('error','Reason required','A block is recorded with who, when and why.'); return; }
            close();
            try {
              const res = await Store.mutate(()=>API.connectors.block(r.id, { reason }), { event:'connectors:changed' });
              toast('warn','Connector blocked', res.message);
              refreshAll();
            } catch (err) { toast('error','Could not block the connector', err.message); }
          }}],
        });
      }

      async function unblockConn(r){
        if(!allowed('admin','Unblocking a connector')) return;
        try {
          const res = await Store.mutate(()=>API.connectors.unblock(r.id), { event:'connectors:changed' });
          toast('success','Connector unblocked', res.message);
          refreshAll();
        } catch (err) { toast('error','Could not unblock the connector', err.message); }
      }

      function editPermissions(r){
        if(!allowed('operator','Editing connector permissions')) return;
        openModal({
          title:'Edit Permissions — '+r.name, icon:'lock',
          body:`<div class="grid g2">
              <div class="form-row"><label>ACCESS LEVEL</label><select class="filter-select w-100" id="cnAccess" style="height:34px">${optionList(['Read','Read-Write','Admin'], r.access)}</select></div>
              <div class="form-row"><label>DATA CLASSIFICATION</label><select class="filter-select w-100" id="cnClass" style="height:34px">${optionList(CLASSIFICATIONS, r.data_classification)}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>RISK LEVEL</label><select class="filter-select w-100" id="cnRisk" style="height:34px">${optionList(RISKS, r.risk_level)}</select></div>
              <div class="form-row"><label>AUTHENTICATION</label><select class="filter-select w-100" id="cnAuth" style="height:34px"><option value="">Not recorded</option>${optionList(AUTH_MODES, r.auth_mode)}</select></div>
            </div>
            <div class="form-row"><label>GRANTED SCOPES (ONE PER LINE)</label>
              <textarea class="input" id="cnScopes" rows="4" placeholder="e.g. Sites.Read.All">${esc((r.scopes||[]).join('\n'))}</textarea></div>`,
          footer:[{label:'Cancel'},{label:'Save Permissions', cls:'primary', onClick: async (close, modal)=>{
            const body = {
              access: modal.querySelector('#cnAccess').value,
              data_classification: modal.querySelector('#cnClass').value,
              risk_level: modal.querySelector('#cnRisk').value,
              auth_mode: modal.querySelector('#cnAuth').value || null,
              scopes: modal.querySelector('#cnScopes').value.split('\n').map(s=>s.trim()).filter(Boolean),
              expected_updated_at: r.updated_at,
            };
            close();
            try {
              const saved = await Store.mutate(()=>API.connectors.update(r.id, body), { event:'connectors:changed' });
              toast('success','Permissions updated', `${saved.name} now grants ${saved.access} on ${saved.data_classification} data.`);
              refreshAll();
              if(currentId === r.id) showConn(saved);
            } catch (err) { toast('error','Could not save the permissions', err.message); }
          }}],
        });
      }

      function grantToAgent(r){
        if(!allowed('operator','Granting a connector to an agent')) return;
        openModal({
          title:'Grant to Agent — '+r.name, icon:'bot',
          body:`<p style="margin-top:0">The agent can call <b style="color:var(--text)">${esc(r.name)}</b> at its recorded access level the moment the grant is recorded.</p>
            <div class="form-row"><label>AGENT</label>
              <select class="filter-select w-100" id="cnGrantAgent" style="height:34px"><option value="">Loading agents…</option></select></div>`,
          footer:[{label:'Cancel'},{label:'Grant Access', cls:'primary', onClick: async (close, modal)=>{
            const select = modal.querySelector('#cnGrantAgent');
            const agentId = select.value;
            if(!agentId){ toast('error','Agent required','Pick the agent that receives this grant.'); return; }
            const agentName = select.options[select.selectedIndex] ? select.options[select.selectedIndex].textContent : 'The agent';
            close();
            try {
              await Store.mutate(()=>API.connectors.grant(r.id, { agent_id: agentId }), { event:'connectors:changed' });
              toast('success','Grant recorded', `${agentName} now holds a grant on ${r.name}.`);
              refreshAll();
              if(currentId === r.id) showConn(r);
            } catch (err) { toast('error','Could not record the grant', err.message); }
          }}],
          onOpen(modal){
            const select = modal.querySelector('#cnGrantAgent');
            API.agents.list({ status:'Active', page_size:100 })
              .then(page => {
                const items = page.items || [];
                select.innerHTML = items.length
                  ? items.map(a=>`<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('')
                  : '<option value="">No active agents to grant to</option>';
              })
              .catch(err => { select.innerHTML = `<option value="">Agents unavailable — ${esc(err.message)}</option>`; });
          },
        });
      }

      document.getElementById('cnAdd').addEventListener('click', ()=>{
        if(!allowed('operator','Registering a connector')) return;
        openModal({
          title:'Add Connector', icon:'plus',
          body:`<div class="form-row"><label>NAME</label><input class="input" id="ncnName" placeholder="e.g. NetSuite ERP Connector"></div>
            <div class="grid g2">
              <div class="form-row"><label>TYPE</label><select class="filter-select w-100" id="ncnType" style="height:34px">${optionList(CONNECTOR_TYPES,'Connector')}</select></div>
              <div class="form-row"><label>ACCESS</label><select class="filter-select w-100" id="ncnAccess" style="height:34px">${optionList(['Read','Read-Write','Admin'],'Read')}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>PROVIDER</label><input class="input" id="ncnProv" placeholder="e.g. Oracle NetSuite"></div>
              <div class="form-row"><label>RISK LEVEL</label><select class="filter-select w-100" id="ncnRisk" style="height:34px">${optionList(RISKS,'Medium')}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>CLASSIFICATION</label><select class="filter-select w-100" id="ncnClass" style="height:34px">${optionList(CLASSIFICATIONS,'Internal')}</select></div>
              <div class="form-row"><label>AUTHENTICATION</label><select class="filter-select w-100" id="ncnAuth" style="height:34px">${optionList(AUTH_MODES)}</select></div>
            </div>
            <div class="form-row"><label>ENDPOINT URL</label><input class="input" id="ncnUrl" placeholder="https://…"></div>
            <div class="form-row"><label>SCOPES (ONE PER LINE)</label><textarea class="input" id="ncnScopes" rows="3" placeholder="e.g. Sites.Read.All"></textarea></div>`,
          footer:[{label:'Cancel'},{label:'Add & Review', cls:'primary', onClick: async (close, modal)=>{
            const name = modal.querySelector('#ncnName').value.trim();
            if(!name){ toast('error','Name required','A connector needs a name before it can be governed.'); return; }
            const body = {
              name,
              connector_type: modal.querySelector('#ncnType').value,
              provider: modal.querySelector('#ncnProv').value.trim() || null,
              risk_level: modal.querySelector('#ncnRisk').value,
              access: modal.querySelector('#ncnAccess').value,
              data_classification: modal.querySelector('#ncnClass').value,
              auth_mode: modal.querySelector('#ncnAuth').value,
              endpoint_url: modal.querySelector('#ncnUrl').value.trim() || null,
              scopes: modal.querySelector('#ncnScopes').value.split('\n').map(s=>s.trim()).filter(Boolean),
            };
            close();
            try {
              const created = await Store.mutate(()=>API.connectors.create(body), { event:'connectors:changed' });
              toast('success','Connector registered', `${created.name} added — no agent holds a grant on it yet.`);
              refreshAll();
            } catch (err) { toast('error','Could not register the connector', err.message); }
          }}],
        });
      });

      /* ---- inspector: four tabs, each off what the API returned ---- */
      function showConn(row){
        const insp = document.getElementById('cnInspector');
        if(!insp || !row) return;
        currentId = row.id;
        document.getElementById('cnLayout').classList.remove('collapsed');
        insp.innerHTML = `<div class="insp-head">
            <span class="entity-ico" style="width:38px;height:38px;background:var(--panel-3)">${logoFor(row.connector_type==='MCP Server'?'mcp':'custom')}</span>
            <div class="grow"><div class="insp-title">${esc(row.name)}</div>
              <div class="insp-sub">${esc(row.provider || row.connector_type || '')}</div></div>
            <button class="icon-btn insp-close" id="cnInspClose">${ICONS.x}</button></div>
          <div class="card-loading" style="height:220px;margin:12px"></div>`;
        insp.querySelector('#cnInspClose').addEventListener('click', ()=>document.getElementById('cnLayout').classList.add('collapsed'));

        API.connectors.get(row.id)
          .then(c => { if(currentId === row.id) paintConn(insp, c); })
          .catch(err => {
            if(currentId !== row.id) return;
            const holder = insp.querySelector('.card-loading');
            if(holder) holder.replaceWith(screenError(err, ()=>showConn(row), 'this connector'));
          });
      }

      function paintConn(insp, c){
        let inspTab = 0;
        insp.innerHTML = `<div class="insp-head">
            <span class="entity-ico" style="width:38px;height:38px;background:var(--panel-3)">${logoFor(c.connector_type==='MCP Server'?'mcp':'custom')}</span>
            <div class="grow"><div class="insp-title">${esc(c.name)}</div>
              <div class="insp-sub">${esc(c.provider || c.connector_type || '')}</div>
              <div style="margin-top:5px">${statusText(c.status)}</div></div>
            <button class="icon-btn insp-close" id="cnInspClose">${ICONS.x}</button></div>
          <div class="tabs" style="margin:10px 0 0">${['Overview','Permissions','Usage','Audit'].map((t,i)=>
            `<div class="tab ${i===0?'active':''}" data-it="${i}" style="padding:8px 10px">${t}</div>`).join('')}</div>
          <div id="cnInspBody"></div>`;
        insp.querySelector('#cnInspClose').addEventListener('click', ()=>document.getElementById('cnLayout').classList.add('collapsed'));

        const bodyEl = insp.querySelector('#cnInspBody');
        function paintTab(i){
          inspTab = i;
          if(i === 0){
            bodyEl.innerHTML = `
              ${c.is_blocked ? `<div class="scan-note">${ICONS.alert} Blocked ${esc(rel(c.blocked_at))}${c.blocked_by?' by '+esc(c.blocked_by):''}${c.blocked_reason?' — '+esc(c.blocked_reason):''}</div>` : ''}
              ${inspSection('General Information','info', kv([
                ['Connector ID', `<span class="mono small">${esc(c.id)}</span>`],
                ['Type', badge(c.connector_type, c.connector_type==='MCP Server'?'purple':'blue')],
                ['Provider', text(c.provider)],
                ['Endpoint', c.endpoint_url ? `<span class="mono small">${esc(c.endpoint_url)}</span>` : dash],
                ['Authentication', text(c.auth_mode)],
                ['Status', statusText(c.status)],
                ['Registered', `${day(c.created_at)}${c.created_by?' · '+esc(c.created_by):''}`],
                ['Last Modified', `${when(c.updated_at)}${c.updated_by?' · '+esc(c.updated_by):''}`],
                ['Last Used', rel(c.last_used_at)],
              ]))}
              ${inspSection('Risk & Compliance','shield', kv([
                ['Risk Level', c.risk_level?riskBadge(c.risk_level):dash],
                ['Data Classification', c.data_classification?badge(c.data_classification):dash],
                ['Crosses Tenant Boundary', c.is_external ? '<span class="st-amber">Yes</span>' : 'No'],
                ['Blocked', c.is_blocked ? '<span class="st-red">Yes</span>' : 'No'],
                ...(c.blocked_reason ? [['Block Reason', `<span class="st-red">${esc(c.blocked_reason)}</span>`]] : []),
              ]))}
              ${inspSection('Used By Agents ('+(c.agents||[]).length+')','bot',
                ((c.agents||[]).length
                  ? c.agents.map(a=>`<div class="flex" style="gap:8px;padding:4px 0">${avatarHtml(a.name, true)}
                      <span class="link grow" data-nav="agent/${esc(a.id)}" style="font-size:12px">${esc(a.name)}</span>
                      <button class="btn sm" data-revoke="${esc(a.id)}">Revoke</button></div>`).join('')
                  : '<span class="faint small">No agent holds a grant on this connector.</span>')
                + `<button class="link" data-nav="agents" style="margin-top:6px">View all agents ${ICONS.arrowRight}</button>`)}
              <div class="insp-section"><div class="flex" style="gap:8px">
                <button class="btn sm grow" style="justify-content:center" id="cnEditBtn">Edit Permissions</button>
                <button class="btn sm grow" style="justify-content:center" id="cnTestBtn">Test Connection</button></div></div>`;
            requireRole(bodyEl.querySelector('#cnEditBtn'), 'operator', 'Editing connector permissions')
              .addEventListener('click', ()=>editPermissions(c));
            requireRole(bodyEl.querySelector('#cnTestBtn'), 'member', 'Testing a connector')
              .addEventListener('click', ()=>testConn(c));
            bodyEl.querySelectorAll('[data-revoke]').forEach(btn=>{
              const agent = (c.agents||[]).find(a=>a.id === btn.dataset.revoke);
              requireRole(btn, 'operator', 'Revoking a connector grant').addEventListener('click', async ()=>{
                if(!allowed('operator','Revoking a connector grant')) return;
                try {
                  await Store.mutate(()=>API.connectors.revokeGrant(c.id, btn.dataset.revoke), { event:'connectors:changed' });
                  toast('success','Grant revoked', `${agent ? agent.name : 'The agent'} no longer holds a grant on ${c.name}.`);
                  refreshAll();
                  showConn(c);
                } catch (err) { toast('error','Could not revoke the grant', err.message); }
              });
            });
          }
          else if(i === 1){
            bodyEl.innerHTML = `
              ${inspSection('Granted Scopes','lock', (c.scopes||[]).length
                ? c.scopes.map(s=>`<div class="flex" style="gap:8px;padding:4px 0">
                    <span style="color:#15803D;width:13px;display:inline-flex">${ICONS.check}</span>
                    <span style="font-size:12px">${esc(s)}</span></div>`).join('')
                : '<span class="faint small">No scopes recorded on this connector.</span>')}
              ${inspSection('Restrictions','shield', [
                  c.is_blocked ? 'Blocked for every agent in this workspace' : null,
                  c.access === 'Read' ? 'Read-only: no writes are permitted through this grant' : null,
                  c.data_classification === 'Internal' ? 'Internal data only — must not carry data past the tenant boundary' : null,
                ].filter(Boolean).map(s=>`<div class="flex" style="gap:8px;padding:4px 0">
                    <span style="color:#B91C1C;width:13px;display:inline-flex">${ICONS.x}</span>
                    <span style="font-size:12px">${esc(s)}</span></div>`).join('')
                || '<span class="faint small">No restrictions beyond the access level below.</span>')}
              ${inspSection('Tool Permissions','tool', kv([
                ['Access Level', c.access?badge(c.access, c.access==='Admin'?'red':c.access==='Read-Write'?'amber':'green'):dash],
                ['Classification Ceiling', c.data_classification?badge(c.data_classification):dash],
                ['Agents Granted', num((c.agents||[]).length)],
                ['Owner User ID', c.owner_user_id ? '<span class="mono small">'+esc(c.owner_user_id)+'</span>' : dash],
              ]))}`;
          }
          else if(i === 2){
            bodyEl.innerHTML = `
              ${inspSection('Usage','chart', `<div class="grid g3" style="gap:8px;margin-bottom:8px">
                ${[['Agents Granted', String((c.agents||[]).length)],
                   ['Last Used', rel(c.last_used_at)],
                   ['Status', esc(c.status)]].map(x=>
                  `<div style="background:var(--panel-2);border:1px solid var(--border-soft);border-radius:8px;padding:8px">
                    <div class="small faint">${x[0]}</div><div style="font-weight:700;margin-top:2px;font-size:12px">${x[1]}</div></div>`).join('')}
              </div>
              <div class="faint small">The registry records grants and reachability. Per-call volume, latency and payload sizes are held with the runs that made the calls — open Live Runs filtered by the agent to see them.</div>
              <button class="link" data-nav="live-runs" style="margin-top:6px">Open Live Runs ${ICONS.arrowRight}</button>`)}
              ${inspSection('Reachability','activity', `<div id="cnProbe"><span class="faint small">Run a test to record the latest probe.</span></div>
                <button class="btn sm mt" id="cnProbeBtn">${ICONS.activity}Test Connection</button>`)}`;
            const probeBtn = bodyEl.querySelector('#cnProbeBtn');
            requireRole(probeBtn, 'member', 'Testing a connector').addEventListener('click', async ()=>{
              if(!allowed('member','Testing a connector')) return;
              const out = bodyEl.querySelector('#cnProbe');
              out.innerHTML = '<div class="card-loading" style="height:44px"></div>';
              try {
                const res = await API.connectors.test(c.id);
                out.innerHTML = kv([
                  ['Result', res.ok ? '<span class="st-green">Reachable</span>' : '<span class="st-red">'+esc(res.status)+'</span>'],
                  ['Latency', res.latency_ms == null ? dash : res.latency_ms + ' ms'],
                  ['HTTP Status', res.http_status == null ? dash : String(res.http_status)],
                  ['Checked', when(res.checked_at)],
                  ['Detail', esc(res.message)],
                ]);
                table.refresh();
              } catch (err) { out.innerHTML = ''; out.appendChild(screenError(err, null, 'the probe result')); }
            });
          }
          else {
            bodyEl.innerHTML = inspSection('Recent Audit Events','history', '<div class="card-loading" style="height:120px"></div>');
            const host = bodyEl.querySelector('.insp-section');
            API.audit.list({ entity_id: c.id, page_size: 8, sort:'-occurred_at' })
              .then(page => {
                if(inspTab !== 3) return;
                const rows = page.items || [];
                host.innerHTML = `<div class="insp-section-title">${ICONS.history}Recent Audit Events</div>` + (rows.length
                  ? rows.map(e=>`<div class="kv"><span class="k">${esc(e.action)}</span>
                      <span class="v small">${esc(e.detail || '—')} · <span class="faint">${esc(rel(e.occurred_at))}</span></span></div>`).join('')
                  : '<span class="faint small">No audit events for this connector yet.</span>');
              })
              .catch(err => {
                if(inspTab !== 3) return;
                host.innerHTML = `<div class="insp-section-title">${ICONS.history}Recent Audit Events</div>`;
                host.appendChild(screenError(err, ()=>paintTab(3), 'the audit events'));
              });
          }
        }
        insp.querySelectorAll('[data-it]').forEach(tb=>tb.addEventListener('click', ()=>{
          insp.querySelectorAll('[data-it]').forEach(x=>x.classList.remove('active'));
          tb.classList.add('active');
          paintTab(parseInt(tb.dataset.it, 10));
        }));
        paintTab(0);
      }

      loadSummary();
    },
  };

  /* ================================ POLICY CENTER ================================ */

  SCREENS['policies'] = {
    title:'Policy Center',
    render(main){
      let currentId = null;
      const scope = {};   // the category tab, read on every request the table makes

      main.innerHTML = `
        ${pageHead({title:'Policy Center', sub:'Create, manage, and enforce security, access, guardrail, and routing policies across your AI agents and tools.',
          actions:`${searchBox('plSearch','Search policies…')}
          <button class="btn" id="plImport">${ICONS.upload}Import Policy</button>
          <button class="btn" id="plExport">${ICONS.download}Export</button>
          <button class="btn primary" id="plNew">${ICONS.plus}Create Policy</button>`})}
        <div id="plTabs"></div>
        <div id="plKpis">${kpiSkeleton(['Total Policies','Active Policies','Warning','Blocked Actions (30d)','Policies Violated (30d)','Pending Review'])}</div>
        <div class="with-inspector" id="plLayout">
          <div id="plTableWrap"></div>
          <div class="inspector" id="plInspector"></div>
        </div>`;

      requireRole(document.getElementById('plNew'), 'admin', 'Creating a policy');
      requireRole(document.getElementById('plImport'), 'admin', 'Importing policies');

      function loadSummary(){
        const host = document.getElementById('plKpis');
        if(!host) return;
        API.policies.summary()
          .then(s => {
            if(!document.getElementById('plKpis')) return;
            host.innerHTML = kpiRow([
              {label:'Total Policies', value:num(s.total), sub:`${num(s.inactive)} inactive`, icon:'shield', color:'purple'},
              {label:'Active Policies', value:`<span class="st-green">${num(s.active)}</span>`, sub:s.active_pct + '% of total', icon:'checkCircle', color:'green'},
              {label:'Warning', value:`<span class="st-amber">${num(s.warning)}</span>`, sub:s.warning?'Enforcing with warnings':'None', icon:'alert', color:'amber'},
              {label:`Blocked Actions (${s.window_days}d)`, value:num(s.blocked_actions_30d), sub:'Enforced as Block', icon:'xCircle', color:'red'},
              {label:`Policies Violated (${s.window_days}d)`, value:num(s.policies_violated_30d), sub:`${num(s.violations_30d)} violation events`, icon:'flag', color:'orange'},
              {label:'Pending Review', value:num(s.pending_review), sub:s.pending_review?'Awaiting governance sign-off':'None', icon:'clock', color:'blue'},
            ], 190);
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSummary, 'the policy summary')); });
      }

      /* ---- category tabs: each one narrows the server query ---- */
      let activeTab = 0;
      const tabsHost = document.getElementById('plTabs');
      tabBar(tabsHost, [{label:'All Policies'}].concat(POLICY_CATEGORIES.map(c=>({label:c}))), i=>{
        activeTab = i;
        if(i === 0) delete scope.category; else scope.category = POLICY_CATEGORIES[i-1];
        syncCategoryFilter();
        table.state.page = 1;
        table.refresh();
      }, activeTab);

      /* extraParams overwrites the same-named query param, so while a category
         tab is active the Category dropdown is parked rather than silently ignored. */
      function syncCategoryFilter(){
        const sel = table.filterEl && table.filterEl.querySelector('[data-fi="1"]');
        if(!sel) return;
        const pinned = !!scope.category;
        sel.disabled = pinned;
        sel.title = pinned ? `The ${scope.category} tab is already filtering by category.` : '';
        if(pinned && sel.value){ sel.value = ''; table.state.filters.category = ''; }
      }

      /* ---- the policy table ---- */
      const table = dataTable({
        columns:[
          {key:'name', label:'Policy Name', render:r=>`<div><div class="cell-main">${esc(r.name)}</div>
            <div class="cell-sub">${esc(r.description ? (r.description.length>52 ? r.description.slice(0,52)+'…' : r.description) : '—')}</div></div>`},
          {key:'category', label:'Category', render:r=>badge(r.category, CAT_COLORS[r.category]||'gray')},
          {key:'scope', label:'Scope', render:r=>dim(r.scope_label || r.scope)},
          {key:'risk_level', label:'Risk Level', render:r=>r.risk_level?riskBadge(r.risk_level):dash},
          {key:'status', label:'Status', render:r=>statusText(r.status, r.status==='Active'?'green':r.status==='Warning'?'amber':r.status==='Inactive'?'gray':'purple')},
          {key:'enforcement', label:'Enforcement', render:r=>badge(r.enforcement, ENF_COLORS[r.enforcement]||'gray')},
          {key:'version', label:'Version', render:r=>`<span class="mono small">${esc(r.version)}</span>`},
          {key:'violations_30d', label:'Violations (30d)', align:'right', cls:'num', render:r=>num(r.violations_30d)},
          {key:'updated_at', label:'Last Modified', render:r=>`<div class="dim nowrap">${rel(r.updated_at)}</div>
            <div class="cell-sub">${esc(r.updated_by || '—')}</div>`},
        ],
        rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'policies', selectable:true,
        searchPlaceholder:'Search policies by name, description, scope or version…',
        defaultSort:{key:'updated_at', dir:-1},
        emptyText:'No policies defined yet',
        filters:[
          {key:'status', label:'Status', param:'status', options:POLICY_STATES, allLabel:'All Status'},
          {key:'category', label:'Category', param:'category', options:POLICY_CATEGORIES, allLabel:'All Categories'},
          {key:'scope', label:'Scope', param:'scope', options:POLICY_SCOPES, allLabel:'All Scopes'},
          {key:'risk', label:'Risk Level', param:'risk', options:RISKS, allLabel:'All Risk Levels'},
          {key:'enforcement', label:'Enforcement', param:'enforcement', options:POLICY_ENFORCEMENTS, allLabel:'All Enforcement'},
        ],
        extraParams: scope,
        source:(params)=>API.policies.list(params),
        exportSource:(params)=>API.policies.export(params),
        // The table passes the row element as a second argument; the inspector's
        // opening tab must not be taken from it.
        onSelect: (row)=>showPolicy(row),
        // Rows arrive after construction and again on every category tab, so the
        // first row is chosen here rather than by an immediate selectFirst().
        onLoad:(rows)=>{
          if(!rows.length){
            currentId = null;
            const insp = document.getElementById('plInspector');
            if(insp) insp.innerHTML = EMPTY('shield','Nothing selected','Create a policy, or clear the filters, to inspect one here.');
            return;
          }
          if(!currentId || !rows.some(r=>r.id === currentId)) table.selectFirst();
        },
        rowActions: r=>[
          {label:'Edit Policy', icon:'edit', onClick:()=>editPolicy(r)},
          {label:'Clone Policy', icon:'copy', onClick:()=>clonePolicy(r)},
          {label:'View Violations', icon:'flag', onClick:()=>showPolicy(r, 3)},
          {sep:true},
          r.status === 'Active'
            ? {label:'Deactivate Policy', icon:'xCircle', danger:true, onClick:()=>deactivatePolicy(r)}
            : {label:'Activate Policy', icon:'checkCircle', onClick:()=>activatePolicy(r)},
        ],
      });

      const wrap = document.getElementById('plTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);

      document.getElementById('plSearch').addEventListener('input', e=>table.search(e.target.value));
      document.getElementById('plExport').addEventListener('click', ()=>table.export());

      function refreshAll(){ table.refresh(); loadSummary(); }

      /* ---- mutations ---- */
      async function activatePolicy(r){
        if(!allowed('admin','Activating a policy')) return;
        try {
          const res = await Store.mutate(()=>API.policies.activate(r.id), { event:'policies:changed' });
          toast('success','Policy activated', res.message);
          refreshAll();
          if(currentId === r.id) showPolicy(res.policy);
        } catch (err) { toast('error','Could not activate', err.message); }
      }

      function deactivatePolicy(r){
        if(!allowed('admin','Deactivating a policy')) return;
        confirmModal({ title:'Deactivate Policy', danger:true, confirmLabel:'Deactivate',
          body:`<p style="margin-top:0">Enforcement of <b style="color:var(--text)">${esc(r.name)}</b> stops immediately${
            r.applies_agents ? ` for <b style="color:var(--text)">${r.applies_agents}</b> agent(s)` : ''}.</p>
            <div class="form-row" style="margin-top:10px"><label>REASON (RECORDED)</label>
              <input class="input" id="plDeactReason" placeholder="Why is this policy being switched off?"></div>`,
          onConfirm: async (modal)=>{
            const reason = modal ? (modal.querySelector('#plDeactReason')||{}).value.trim() : '';
            try {
              const res = await Store.mutate(()=>API.policies.deactivate(r.id, reason ? { reason } : {}), { event:'policies:changed' });
              toast('warn','Policy deactivated', res.message);
              refreshAll();
              if(currentId === r.id) showPolicy(res.policy);
            } catch (err) { toast('error','Could not deactivate', err.message); }
          }});
      }

      async function clonePolicy(r){
        if(!allowed('admin','Cloning a policy')) return;
        try {
          const res = await Store.mutate(()=>API.policies.clone(r.id), { event:'policies:changed' });
          toast('success','Policy cloned', res.message);
          refreshAll();
        } catch (err) { toast('error','Could not clone', err.message); }
      }

      function policyForm(p){
        return `<div class="form-row"><label>POLICY NAME</label><input class="input" id="pfName" value="${esc(p ? p.name : '')}" placeholder="e.g. Block External File Sharing"></div>
          <div class="grid g2">
            <div class="form-row"><label>CATEGORY</label><select class="filter-select w-100" id="pfCat" style="height:34px">${optionList(POLICY_CATEGORIES, p ? p.category : 'Guardrails')}</select></div>
            <div class="form-row"><label>ENFORCEMENT</label><select class="filter-select w-100" id="pfEnf" style="height:34px">${optionList(POLICY_ENFORCEMENTS, p ? p.enforcement : 'Block')}</select></div>
          </div>
          <div class="grid g2">
            <div class="form-row"><label>SCOPE</label><select class="filter-select w-100" id="pfScope" style="height:34px">${optionList(POLICY_SCOPES, p ? p.scope : 'Global')}</select></div>
            <div class="form-row"><label>SCOPE TARGET</label><input class="input" id="pfScopeRef" value="${esc(p ? (p.scope_ref||'') : '')}" placeholder="Agent id, connector id or environment"></div>
          </div>
          <div class="grid g2">
            <div class="form-row"><label>RISK LEVEL</label><select class="filter-select w-100" id="pfRisk" style="height:34px">${optionList(RISKS, p ? p.risk_level : 'Medium')}</select></div>
            <div class="form-row"><label>STATUS</label><select class="filter-select w-100" id="pfStatus" style="height:34px">${optionList(POLICY_STATES, p ? p.status : 'Active')}</select></div>
          </div>
          <div class="form-row"><label>DESCRIPTION</label><textarea class="input" id="pfDesc" rows="2" placeholder="What does this policy enforce?">${esc(p ? (p.description||'') : '')}</textarea></div>
          <div class="form-row"><label>RULE BODY (JSON — ${p ? 'LEAVE BLANK TO KEEP THE CURRENT RULES' : 'LEAVE BLANK TO DERIVE ONE'})</label>
            <textarea class="input" id="pfRules" rows="6" style="font-family:Consolas,monospace;font-size:11.5px">${
              p && p.rules && Object.keys(p.rules).length ? esc(JSON.stringify(p.rules, null, 2)) : ''}</textarea></div>`;
      }

      /** Read the shared policy form, or throw a message a person can act on. */
      function readPolicyForm(modal){
        const name = modal.querySelector('#pfName').value.trim();
        if(name.length < 2) throw new Error('A policy needs a name of at least two characters.');
        const scopeVal = modal.querySelector('#pfScope').value;
        const scopeRef = modal.querySelector('#pfScopeRef').value.trim();
        if(scopeVal !== 'Global' && !scopeRef) throw new Error(`A ${scopeVal} scope needs a target — the agent, connector or environment it applies to.`);
        const rulesText = modal.querySelector('#pfRules').value.trim();
        let rules = null;
        if(rulesText){
          try { rules = JSON.parse(rulesText); }
          catch (e) { throw new Error('The rule body is not valid JSON: ' + e.message); }
        }
        const body = {
          name,
          category: modal.querySelector('#pfCat').value,
          enforcement: modal.querySelector('#pfEnf').value,
          scope: scopeVal,
          scope_ref: scopeVal === 'Global' ? null : scopeRef,
          risk_level: modal.querySelector('#pfRisk').value,
          status: modal.querySelector('#pfStatus').value,
          description: modal.querySelector('#pfDesc').value.trim() || null,
        };
        if(rules) body.rules = rules;
        return body;
      }

      document.getElementById('plNew').addEventListener('click', ()=>{
        if(!allowed('admin','Creating a policy')) return;
        openModal({
          title:'Create Policy', icon:'shield', wide:true,
          body: policyForm(null),
          footer:[{label:'Cancel'},{label:'Create Policy', cls:'primary', onClick: async (close, modal)=>{
            let body;
            try { body = readPolicyForm(modal); }
            catch (err) { toast('error','Check the form', err.message); return; }
            try {
              const created = await Store.mutate(()=>API.policies.create(body), { event:'policies:changed' });
              close();
              toast('success','Policy created', `${created.name} is ${created.status === 'Active' ? 'now enforced' : 'saved as ' + created.status}.`);
              refreshAll();
            } catch (err) { toast('error','Could not create the policy', err.message); }
          }}],
        });
      });

      function editPolicy(r){
        if(!allowed('admin','Editing a policy')) return;
        openModal({
          title:'Edit Policy — '+r.name, icon:'edit', wide:true,
          body:`<div class="card-loading" style="height:260px"></div>`,
          footer:[{label:'Cancel'},{label:'Save Changes', cls:'primary', onClick: async (close, modal)=>{
            let body;
            try { body = readPolicyForm(modal); }
            catch (err) { toast('error','Check the form', err.message); return; }
            body.expected_updated_at = modal.dataset.updatedAt || null;
            try {
              const saved = await Store.mutate(()=>API.policies.update(r.id, body), { event:'policies:changed' });
              close();
              toast('success','Policy updated', `${saved.name} saved as ${saved.version}.`);
              refreshAll();
              if(currentId === r.id) showPolicy(saved);
            } catch (err) { toast('error','Could not save the policy', err.message); }
          }}],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.policies.get(r.id)
              .then(p => { body.innerHTML = policyForm(p); modal.dataset.updatedAt = p.updated_at; })
              .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, null, 'this policy')); });
          },
        });
      }

      document.getElementById('plImport').addEventListener('click', ()=>{
        if(!allowed('admin','Importing policies')) return;
        openModal({
          title:'Import Policy Definitions', icon:'upload', wide:true,
          body:`<p style="margin-top:0" class="small muted">Paste a policy definition file, or choose one — a JSON array of policies, or an object with a <span class="mono">policies</span> array.</p>
            <div class="form-row"><label>FILE</label><input type="file" id="plImpFile" accept=".json,application/json"></div>
            <div class="form-row"><label>DEFINITIONS</label><textarea class="input" id="plImpText" rows="10" style="font-family:Consolas,monospace;font-size:11.5px" placeholder='[{"name":"Block External File Sharing","category":"Data Protection","enforcement":"Block"}]'></textarea></div>
            <div class="grid g2">
              <div class="form-row"><label>ON CONFLICT</label><select class="filter-select w-100" id="plImpConflict" style="height:34px">${optionList(['skip','replace','fail'],'skip')}</select></div>
              <label class="flex" style="gap:8px;font-size:12.5px;align-items:center"><input type="checkbox" id="plImpActivate"> Activate every imported policy</label>
            </div>`,
          footer:[{label:'Cancel'},{label:'Import', cls:'primary', onClick: async (close, modal)=>{
            const raw = modal.querySelector('#plImpText').value.trim();
            if(!raw){ toast('error','Nothing to import','Paste or choose a policy definition file first.'); return; }
            let parsed;
            try { parsed = JSON.parse(raw); }
            catch (err) { toast('error','That is not valid JSON', err.message); return; }
            const policies = Array.isArray(parsed) ? parsed : parsed.policies;
            if(!Array.isArray(policies) || !policies.length){
              toast('error','No definitions found','The file must hold a policies array with at least one entry.');
              return;
            }
            const body = { policies, on_conflict: modal.querySelector('#plImpConflict').value,
              activate: modal.querySelector('#plImpActivate').checked,
              source: modal.dataset.filename || 'Pasted definitions' };
            close();
            try {
              const res = await Store.mutate(()=>API.policies.import(body), { event:'policies:changed' });
              const issues = res.issues || [];
              toast(issues.length ? 'warn' : 'success', 'Import complete',
                `${res.created} created, ${res.replaced} replaced, ${res.skipped} skipped of ${res.submitted}.`);
              if(issues.length) openModal({ title:'Import Issues', icon:'alert',
                body:`<p style="margin-top:0">${issues.length} definition(s) were not imported:</p>` +
                  kv(issues.map(i=>[`#${i.index + 1} ${i.name}`, esc(i.reason)])),
                footer:[{label:'Close'}] });
              refreshAll();
            } catch (err) { toast('error','Could not import', err.message); }
          }}],
          onOpen(modal){
            modal.querySelector('#plImpFile').addEventListener('change', e=>{
              const file = e.target.files && e.target.files[0];
              if(!file) return;
              modal.dataset.filename = file.name;
              const reader = new FileReader();
              reader.onload = ()=>{ modal.querySelector('#plImpText').value = String(reader.result || ''); };
              reader.onerror = ()=>toast('error','Could not read the file', file.name + ' could not be opened.');
              reader.readAsText(file);
            });
          },
        });
      });

      /* ---- inspector: five tabs ---- */
      function showPolicy(row, openTab){
        const insp = document.getElementById('plInspector');
        if(!insp || !row) return;
        currentId = row.id;
        document.getElementById('plLayout').classList.remove('collapsed');
        insp.innerHTML = `<div class="insp-head"><div class="grow">
            <div class="insp-title" style="display:flex;align-items:center;gap:8px">${esc(row.name)}</div></div>
            <button class="icon-btn insp-close" id="plInspClose">${ICONS.x}</button></div>
          <div class="card-loading" style="height:240px;margin:12px"></div>`;
        insp.querySelector('#plInspClose').addEventListener('click', ()=>document.getElementById('plLayout').classList.add('collapsed'));

        API.policies.get(row.id)
          .then(p => { if(currentId === row.id) paintPolicy(insp, p, openTab || 0); })
          .catch(err => {
            if(currentId !== row.id) return;
            const holder = insp.querySelector('.card-loading');
            if(holder) holder.replaceWith(screenError(err, ()=>showPolicy(row, openTab), 'this policy'));
          });
      }

      function paintPolicy(insp, p, openTab){
        let tab = openTab;
        insp.innerHTML = `<div class="insp-head"><div class="grow">
            <div class="insp-title" style="display:flex;align-items:center;gap:8px">${esc(p.name)}
              ${statusText(p.status, p.status==='Active'?'green':p.status==='Warning'?'amber':p.status==='Inactive'?'gray':'purple')}</div>
            <div style="margin-top:6px">${badge(p.category, CAT_COLORS[p.category]||'gray')}
              ${badge(p.enforcement, ENF_COLORS[p.enforcement]||'gray')}</div></div>
            <button class="icon-btn insp-close" id="plInspClose">${ICONS.x}</button></div>
          <div class="tabs" style="margin:10px 0 0">${['Overview','Rules','Scope','Violations','History'].map((t,i)=>
            `<div class="tab ${i===openTab?'active':''}" data-it="${i}" style="padding:8px 10px">${t}</div>`).join('')}</div>
          <div id="plInspBody"></div>`;
        insp.querySelector('#plInspClose').addEventListener('click', ()=>document.getElementById('plLayout').classList.add('collapsed'));
        const bodyEl = insp.querySelector('#plInspBody');

        function paintTab(i){
          tab = i;
          if(i === 0){
            bodyEl.innerHTML = `
              ${inspSection('Description','info', `<div class="quote">${p.description ? esc(p.description) : 'No description recorded.'}</div>`)}
              ${inspSection('Policy Information','fileText', kv([
                ['Policy ID', `<span class="mono small">${esc(p.id)}</span>`],
                ['Version', `<span class="mono">${esc(p.version)}</span>`],
                ['Created By', text(p.created_by)],
                ['Created On', day(p.created_at)],
                ['Last Modified', `${when(p.updated_at)}${p.updated_by?' · '+esc(p.updated_by):''}`],
                ['Enforcement Mode', badge(p.enforcement, ENF_COLORS[p.enforcement]||'gray')],
                ['Risk Level', p.risk_level?riskBadge(p.risk_level):dash],
                ['Last Evaluated', rel(p.last_evaluated_at)],
                ['Last Triggered', rel(p.last_triggered_at)],
              ]) + `<div style="margin-top:8px"><span class="tag">${esc(p.risk_level)} Risk</span><span class="tag">${esc(p.category)}</span><span class="tag">${esc(p.scope_label || p.scope)}</span></div>`)}
              ${inspSection('Applies To','target', `<div class="grid g4" style="gap:8px;text-align:center">
                ${[['Agents',p.applies_agents,'bot'],['Tools',p.applies_tools,'tool'],['Connectors',p.applies_connectors,'link'],['Environments',p.applies_envs,'layers']].map(x=>
                  `<div style="background:var(--panel-2);border:1px solid var(--border-soft);border-radius:9px;padding:9px 4px">
                    <span style="width:15px;display:inline-flex;color:var(--purple-bright)">${ICONS[x[2]]}</span>
                    <div style="font-size:16px;font-weight:700;margin-top:3px">${x[1] == null ? '—' : x[1]}</div>
                    <div class="small faint">${x[0]}</div></div>`).join('')}</div>`)}
              ${inspSection('Summary (Last 30 Days)','chart', kv([
                ['Requests Triggered', num(p.requests_30d)],
                ['Approved', p.approved_pct == null ? dash : `<span class="st-green">${p.approved_pct}%</span>`],
                ['Violations', `<span class="st-amber">${num(p.violations_30d)}</span>`],
                ['Blocked', `<span class="st-red">${num(p.blocked_30d)}</span>`],
                ['Last Triggered', rel(p.last_triggered_at)],
              ]))}
              <div class="insp-section"><div class="insp-section-title">Quick Actions</div>
                <div style="display:flex;flex-direction:column;gap:8px">
                  <button class="btn sm block" id="plEditBtn">${ICONS.edit}Edit Policy</button>
                  <button class="btn sm block" id="plCloneBtn">${ICONS.copy}Clone Policy</button>
                  <button class="btn sm block ${p.status === 'Active' ? 'ghost-danger' : ''}" id="plToggleBtn">${
                    p.status === 'Active' ? ICONS.xCircle + 'Deactivate Policy' : ICONS.checkCircle + 'Activate Policy'}</button>
                </div></div>`;
            requireRole(bodyEl.querySelector('#plEditBtn'), 'admin', 'Editing a policy').addEventListener('click', ()=>editPolicy(p));
            requireRole(bodyEl.querySelector('#plCloneBtn'), 'admin', 'Cloning a policy').addEventListener('click', ()=>clonePolicy(p));
            requireRole(bodyEl.querySelector('#plToggleBtn'), 'admin', 'Changing a policy state')
              .addEventListener('click', ()=>{ if(p.status === 'Active') deactivatePolicy(p); else activatePolicy(p); });
          }
          else if(i === 1){
            const rules = p.rules || {};
            const conditions = rules.conditions || [];
            bodyEl.innerHTML = inspSection('Rule Definition','settings',
              Object.keys(rules).length
                ? `<div class="quote" style="font-family:Consolas,monospace;font-size:11px;white-space:pre-wrap">${esc(JSON.stringify(rules, null, 2))}</div>`
                : '<span class="faint small">No rule body is stored on this policy.</span>')
              + inspSection('Conditions','filter', conditions.length
                ? conditions.map(c=>`<div class="kv"><span class="k">${esc(c.signal)}</span>
                    <span class="v"><span class="mono small">${esc(c.operator)}</span> ${esc(JSON.stringify(c.value))}</span></div>`).join('')
                : '<span class="faint small">This policy has no conditions, so it matches nothing.</span>')
              + inspSection('Evaluation Order','layers', kv([
                ['Combining Logic', rules.match === 'any' ? 'Any condition matches' : 'All conditions must match'],
                ['Severity', text(rules.severity)],
                ['Fail Mode', rules.fail_mode === 'open' ? 'Fail open' : 'Fail closed'],
                ['Exceptions', (rules.exceptions||[]).length ? esc(rules.exceptions.join(', ')) : 'None'],
                ['Notify', rules.action && (rules.action.notify||[]).length ? esc(rules.action.notify.join(', ')) : dash],
                ['Audit Every Match', rules.action && rules.action.audit === false ? 'No' : 'Yes'],
              ]));
          }
          else if(i === 2){
            bodyEl.innerHTML = inspSection('Scope','target', kv([
                ['Scope Class', text(p.scope)],
                ['Scope Label', text(p.scope_label)],
                ['Scope Target', p.scope_ref ? `<span class="mono small">${esc(p.scope_ref)}</span>` : dash],
                ['Agents In Scope', num(p.applies_agents)],
                ['Tools In Scope', num(p.applies_tools)],
                ['Connectors In Scope', num(p.applies_connectors)],
                ['Environments', num(p.applies_envs)],
              ]) + (p.scope === 'Agent' && p.scope_ref
                ? `<button class="link" data-nav="agent/${esc(p.scope_ref)}" style="margin-top:8px">Open the agent this binds to ${ICONS.arrowRight}</button>`
                : `<button class="link" data-nav="agents" style="margin-top:8px">Open the Agent Registry ${ICONS.arrowRight}</button>`));
          }
          else if(i === 3){
            bodyEl.innerHTML = inspSection('Violations','flag','<div class="card-loading" style="height:120px"></div>');
            const host = bodyEl.querySelector('.insp-section');
            API.policies.violations({ policy_id: p.id, page_size: 6, sort:'-occurred_at' })
              .then(page => {
                if(tab !== 3) return;
                const rows = page.items || [];
                host.innerHTML = `<div class="insp-section-title">${ICONS.flag}Violations (${page.total})</div>` + (rows.length
                  ? rows.map(v=>`<div style="border:1px solid var(--border-soft);border-radius:9px;padding:9px 11px;margin-bottom:8px;background:var(--panel-2)">
                      <div class="flex between"><b style="font-size:12px">${esc(v.agent_name || 'Unattributed')}</b>${badge(v.severity)}</div>
                      <div class="small dim" style="margin-top:3px">${esc(v.action_taken)} · ${esc(rel(v.occurred_at))}${
                        v.resolved ? ' · <span class="st-green">resolved</span>' : ''}</div>
                      ${v.agent_id?`<button class="link small" data-nav="agent/${esc(v.agent_id)}">View agent ${ICONS.arrowRight}</button>`:''}
                    </div>`).join('')
                  : '<div class="empty-state" style="padding:20px">'+ICONS.checkCircle+'<div class="es-title">No violations</div><div>This policy has not been breached.</div></div>');
              })
              .catch(err => {
                if(tab !== 3) return;
                host.innerHTML = `<div class="insp-section-title">${ICONS.flag}Violations</div>`;
                host.appendChild(screenError(err, ()=>paintTab(3), 'the violations'));
              });
          }
          else {
            bodyEl.innerHTML = inspSection('Change History','history','<div class="card-loading" style="height:120px"></div>');
            const host = bodyEl.querySelector('.insp-section');
            API.audit.list({ entity_id: p.id, page_size: 10, sort:'-occurred_at' })
              .then(page => {
                if(tab !== 4) return;
                const rows = page.items || [];
                host.innerHTML = `<div class="insp-section-title">${ICONS.history}Change History</div>` + (rows.length
                  ? `<div class="pipe">${rows.map(e=>`<div class="pipe-step"><div class="pipe-dot done">${ICONS.check}</div>
                      <div class="pipe-body"><div class="pipe-title"><span>${esc(e.action)}</span></div>
                      <div class="pipe-sub">${esc(e.detail || '—')} · by ${esc(e.actor)} · ${esc(rel(e.occurred_at))}</div>
                      ${e.prev_value || e.new_value ? `<div class="small faint">${esc(e.prev_value || '—')} → ${esc(e.new_value || '—')}</div>` : ''}
                      </div></div>`).join('')}</div>`
                  : '<span class="faint small">No recorded changes yet.</span>');
              })
              .catch(err => {
                if(tab !== 4) return;
                host.innerHTML = `<div class="insp-section-title">${ICONS.history}Change History</div>`;
                host.appendChild(screenError(err, ()=>paintTab(4), 'the change history'));
              });
          }
        }
        insp.querySelectorAll('[data-it]').forEach(tb=>tb.addEventListener('click', ()=>{
          insp.querySelectorAll('[data-it]').forEach(x=>x.classList.remove('active'));
          tb.classList.add('active');
          paintTab(parseInt(tb.dataset.it, 10));
        }));
        paintTab(openTab);
      }

      loadSummary();
    },
  };

  /* ============================= APPROVALS & AUDIT ============================= */

  SCREENS['approvals'] = {
    title:'Approvals & Audit',
    render(main){
      const STATUS_TABS = ['Pending','Approved','Rejected','Escalated'];
      const scope = { status: 'Pending' };   // the queue tab, read on every request
      let currentTab = 0, tabsEl = null, currentId = null, auditTable = null, auditHost = null;
      const seenActions = new Set(), seenAgents = new Set();

      main.innerHTML = `
        ${pageHead({title:'Approvals & Audit', sub:'Manage approval requests, review high-risk actions, and audit all agent activities.',
          actions:`${searchBox('apSearch','Search requests, agents, users, actions…')}
          <button class="btn" id="apExport">${ICONS.download}Export</button>
          <button class="btn primary" id="apNewRule">${ICONS.plus}New Approval Rule</button>`})}
        <div id="apKpis">${kpiSkeleton(['Pending Approvals','Approved (30d)','Rejected (30d)','Escalated (30d)','Avg. Time to Approve','Approval SLA Met'])}</div>
        <div id="apTabs"></div>
        <div class="with-inspector" id="apLayout">
          <div id="apTableWrap"></div>
          <div class="inspector" id="apInspector"></div>
        </div>`;

      requireRole(document.getElementById('apNewRule'), 'admin', 'Creating an approval rule');

      /* ---- KPI cards, and the count the Pending tab carries ---- */
      function loadSummary(){
        const host = document.getElementById('apKpis');
        if(!host) return;
        API.approvals.summary()
          .then(s => {
            if(!document.getElementById('apKpis')) return;
            host.innerHTML = kpiRow([
              {label:'Pending Approvals', value:`<span class="st-amber">${num(s.pending)}</span>`, sub:s.pending?'Awaiting a decision':'Queue is clear', icon:'clock', color:'amber'},
              {label:'Approved (30d)', value:num(s.approved_30d), sub:'Decided in the last 30 days', icon:'checkCircle', color:'green'},
              {label:'Rejected (30d)', value:num(s.rejected_30d), sub:'Decided in the last 30 days', icon:'xCircle', color:'red'},
              {label:'Escalated (30d)', value:num(s.escalated_30d), sub:'Currently with a senior reviewer', icon:'users', color:'orange'},
              {label:'Avg. Time to Approve', value:`<span style="font-size:20px">${s.avg_time_to_approve_seconds == null ? '—' : esc(duration(s.avg_time_to_approve_seconds))}</span>`,
                sub:s.avg_time_to_approve_seconds == null ? 'No approvals in the window' : 'Requested to approved', icon:'clock', color:'blue'},
              {label:'Approval SLA Met', value:s.sla_met_percent == null ? dash : pct(s.sla_met_percent, 0),
                sub:s.sla_met_percent == null ? 'No decisions in the window' : 'Decided inside the SLA', icon:'shield', color:'purple'},
            ], 190);
            paintTabs(s.pending);
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSummary, 'the approval summary')); });
      }

      function paintTabs(pending){
        const host = document.getElementById('apTabs');
        if(!host) return;
        host.innerHTML = '';
        tabsEl = tabBar(host,
          [{label:'Pending', count: pending == null ? null : pending},{label:'Approved'},{label:'Rejected'},{label:'Escalated'},{label:'Audit Trail'}],
          setTab, currentTab);
      }
      paintTabs(null);

      /* ---- the queue ---- */
      const table = dataTable({
        columns:[
          {key:'request_ref', label:'Request ID', render:r=>`<span class="mono" style="color:var(--text)">${esc(r.request_ref)}</span>`},
          {key:'requested_at', label:'Time', render:r=>`<span class="dim nowrap">${rel(r.requested_at)}</span>`},
          {key:'agent', label:'Agent / Source', sortable:false, render:r=>`<div>
            <div class="cell-main">${r.agent_id
              ? `<span class="link" data-nav="agent/${esc(r.agent_id)}" onclick="event.stopPropagation()">${esc(r.agent_name || '—')}</span>`
              : esc(r.agent_name || '—')}</div>
            <div class="cell-sub">${esc(r.agent_platform || r.source || '—')}</div></div>`},
          {key:'action', label:'Action', render:r=>`<div><div class="cell-main" style="font-size:12px">${esc(r.action)}</div>
            <div class="cell-sub">${esc(r.action_detail || '—')}</div></div>`},
          {key:'resource', label:'Resource', render:r=>dim(r.resource)},
          {key:'risk', label:'Risk', render:r=>r.risk?riskBadge(r.risk):dash},
          {key:'policy', label:'Policy', sortable:false, render:r=>r.policy_name?`<span class="dim" style="font-size:11.5px">${esc(r.policy_name)}</span>`:dash},
          {key:'requested_by', label:'Requested By', sortable:false, render:r=>r.requested_by_name?ownerCell(r.requested_by_name, r.requested_by_team||''):dash},
          {key:'sla_due_at', label:'SLA', render:r=>slaCell(r)},
          {key:'status', label:'Status', render:r=>badge(r.status)},
        ],
        rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'requests', selectable:true,
        searchPlaceholder:'Search by reference, action, resource, reason or agent…',
        defaultSort:{key:'requested_at', dir:-1},
        emptyText:'No requests in this queue',
        filters:[
          {key:'agent', label:'Agent', param:'agent', options:[], allLabel:'All Agents'},
          {key:'risk', label:'Risk Level', param:'risk', options:['Low','Medium','High','Critical'], allLabel:'All Risk Levels'},
          {key:'action', label:'Action', param:'action', options:[], allLabel:'All Actions'},
        ],
        extraParams: scope,
        source:(params)=>API.approvals.list(params),
        exportSource:(params)=>API.approvals.export(params),
        onSelect: showRequest,
        // The rows arrive after construction, and again on every tab change, so
        // the first row is chosen here rather than by an immediate selectFirst().
        onLoad:(rows)=>{
          rows.forEach(r=>{ if(r.action) seenActions.add(r.action); if(r.agent_name) seenAgents.add(r.agent_name); });
          fillFilter(table, 0, Array.from(seenAgents).sort());
          fillFilter(table, 2, Array.from(seenActions).sort());
          if(currentTab === 4) return;
          if(!rows.length){ currentId = null; emptyInspector(); return; }
          if(!currentId || !rows.some(r=>r.id === currentId)) table.selectFirst();
        },
        rowActions: r=> r.is_open ? [
          {label:'Review', icon:'eye', onClick:()=>showRequest(r)},
          {label:'Approve', icon:'checkCircle', onClick:()=>decide(r,'approve')},
          {label:'Reject', icon:'xCircle', danger:true, onClick:()=>decide(r,'reject')},
          {label:'Escalate', icon:'users', onClick:()=>decide(r,'escalate')},
        ] : [
          {label:'View Details', icon:'eye', onClick:()=>showRequest(r)},
          {label:'View Audit Trail', icon:'history', onClick:()=>setTab(4)},
        ],
      });

      const wrap = document.getElementById('apTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);

      document.getElementById('apSearch').addEventListener('input', e=>table.search(e.target.value));
      document.getElementById('apExport').addEventListener('click', ()=>{
        if(currentTab === 4 && auditTable) auditTable.export(); else table.export();
      });

      function slaCell(r){
        if(!r.is_open) return r.sla_breached ? badge('Breached','red') : dash;
        if(r.sla_remaining_seconds == null) return r.sla_label ? badge(r.sla_label,'gray') : dash;
        if(r.sla_remaining_seconds < 0) return badge('Overdue ' + duration(-r.sla_remaining_seconds), 'red');
        return badge(duration(r.sla_remaining_seconds) + ' left', r.sla_remaining_seconds < 3600 ? 'amber' : 'gray');
      }

      function emptyInspector(){
        const insp = document.getElementById('apInspector');
        if(insp) insp.innerHTML = EMPTY('checkCircle','Nothing to review here',
          `No ${STATUS_TABS[currentTab] ? STATUS_TABS[currentTab].toLowerCase() : ''} requests right now.`);
      }

      /* ---- tabs: four queues plus the audit trail ---- */
      function setTab(i){
        currentTab = i;
        if(tabsEl) tabsEl.querySelectorAll('.tab').forEach((t,ti)=>t.classList.toggle('active', ti===i));
        if(i === 4){
          table.el.style.display = 'none';
          table.filterEl.style.display = 'none';
          showAudit();
        } else {
          if(auditHost) auditHost.style.display = 'none';
          table.el.style.display = '';
          table.filterEl.style.display = '';
          scope.status = STATUS_TABS[i];
          table.state.page = 1;
          currentId = null;
          table.refresh();
        }
      }

      /* ---- the audit trail tab ---- */
      function showAudit(){
        if(!auditHost){
          auditHost = document.createElement('div');
          auditHost.id = 'apAudit';
          wrap.appendChild(auditHost);
        }
        auditHost.style.display = '';
        if(auditTable){ auditTable.refresh(); paintAuditInspector(); return; }

        auditHost.innerHTML = `<div id="apVerify"></div>`;
        const seenActors = new Set(), seenScreens = new Set();
        auditTable = dataTable({
          columns:[
            {key:'occurred_at', label:'Time', render:r=>`<span class="dim nowrap">${rel(r.occurred_at)}</span>`},
            {key:'actor', label:'Actor', render:r=>ownerCell(r.actor, '')},
            {key:'action', label:'Action', render:r=>`<span class="cell-main">${esc(r.action)}</span>`},
            {key:'entity_type', label:'Entity', render:r=>r.entity_type?`<span class="badge bg-gray">${esc(r.entity_type)}</span>`:dash},
            {key:'entity_label', label:'Resource', sortable:false, render:r=>text(r.entity_label)},
            {key:'detail', label:'Detail', sortable:false, render:r=>dim(r.detail)},
            {key:'source_screen', label:'Source Screen', render:r=>r.source_screen?`<span class="badge bg-gray">${esc(r.source_screen)}</span>`:dash},
            {key:'ip_address', label:'IP', sortable:false, render:r=>r.ip_address?`<span class="mono">${esc(r.ip_address)}</span>`:dash},
          ],
          rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'audit events',
          searchPlaceholder:'Search the audit trail…',
          defaultSort:{key:'occurred_at', dir:-1},
          emptyText:'No audit events recorded yet',
          filters:[
            {key:'actor', label:'Actor', param:'actor', options:[], allLabel:'All Actors'},
            {key:'source_screen', label:'Source Screen', param:'source_screen', options:[], allLabel:'All Screens'},
          ],
          source:(params)=>API.audit.list(params),
          exportSource:(params)=>API.audit.export(params),
          onLoad:(rows)=>{
            rows.forEach(r=>{ if(r.actor) seenActors.add(r.actor); if(r.source_screen) seenScreens.add(r.source_screen); });
            fillFilter(auditTable, 0, Array.from(seenActors).sort());
            fillFilter(auditTable, 1, Array.from(seenScreens).sort());
          },
          onSelect: showAuditEvent,
        });
        auditHost.appendChild(auditTable.filterEl);
        auditHost.appendChild(auditTable.el);
        loadVerify();
        paintAuditInspector();
      }

      /** The banner over the trail: the server replays the hash chain for us. */
      function loadVerify(){
        const host = document.getElementById('apVerify');
        if(!host) return;
        host.innerHTML = `<div class="scan-note">${ICONS.refresh} Verifying the audit hash chain…</div>`;
        API.audit.verify()
          .then(v => {
            if(!document.getElementById('apVerify')) return;
            host.innerHTML = v.intact
              ? `<div class="scan-note">${ICONS.shieldCheck} Hash chain verified — ${fmtFull(v.checked)} event(s) reconcile, newest first. Every row is chained to the one before it.</div>`
              : `<div class="scan-note" style="border-color:var(--red-dim);color:#B91C1C">${ICONS.alert} The hash chain does not reconcile. ${fmtFull(v.checked)} event(s) verified before the break${
                  v.broken_at_event_id ? ` at <span class="mono">${esc(v.broken_at_event_id)}</span>` : ''}${v.broken_at ? ` (${esc(when(v.broken_at))})` : ''}.</div>`;
          })
          .catch(err => {
            if(!document.getElementById('apVerify')) return;
            host.innerHTML = '';
            host.appendChild(screenError(err, loadVerify, 'the chain verification'));
          });
      }

      function paintAuditInspector(){
        const insp = document.getElementById('apInspector');
        if(!insp) return;
        insp.innerHTML = `<div class="insp-head"><div><div class="insp-title">Audit Trail</div>
            <div class="insp-sub">Immutable record of every administrative and agent decision</div></div></div>
          <div id="apAuditMeta"><div class="card-loading" style="height:150px"></div></div>`;
        const meta = insp.querySelector('#apAuditMeta');
        API.audit.verify()
          .then(v => {
            if(!document.getElementById('apAuditMeta')) return;
            meta.innerHTML = inspSection('Integrity','shieldCheck', kv([
                ['Events Verified', fmtFull(v.checked)],
                ['Hash Chain', v.intact ? '<span class="st-green">Verified</span>' : '<span class="st-red">Broken</span>'],
                ...(v.broken_at_event_id ? [['First Break', `<span class="mono small">${esc(v.broken_at_event_id)}</span>`]] : []),
                ...(v.broken_at ? [['Broken At', when(v.broken_at)]] : []),
                ['Checked', 'just now'],
              ]))
              + inspSection('Retention','database', kv([
                ['Write Path', 'Append-only — the API exposes no update or delete'],
                ['Checksum', 'SHA-256 over each row and the row before it'],
                ['Export', 'CSV with checksums, for verification outside this system'],
              ]))
              + `<div class="insp-section"><button class="btn sm block" id="apAuditExport">${ICONS.download}Export the audit trail</button></div>`;
            const btn = document.getElementById('apAuditExport');
            if(btn) btn.addEventListener('click', ()=>{ if(auditTable) auditTable.export(); });
          })
          .catch(err => {
            if(!document.getElementById('apAuditMeta')) return;
            meta.innerHTML = '';
            meta.appendChild(screenError(err, paintAuditInspector, 'the chain status'));
          });
      }

      function showAuditEvent(r){
        const insp = document.getElementById('apInspector');
        if(!insp || !r) return;
        insp.innerHTML = `
          <div class="insp-head"><div class="grow"><div class="insp-title">Audit Event</div>
            <div class="insp-sub mono">${esc(r.id)}</div></div>
            <button class="icon-btn insp-close" id="apAudClose">${ICONS.x}</button></div>
          ${inspSection('Event','history', kv([
            ['Actor', esc(r.actor)],
            ['Action', esc(r.action)],
            ['Entity Type', text(r.entity_type)],
            ['Resource', text(r.entity_label)],
            ['Entity ID', r.entity_id?`<span class="mono small">${esc(r.entity_id)}</span>`:dash],
            ['Timestamp', when(r.occurred_at)],
            ['Source Screen', text(r.source_screen)],
            ['IP Address', r.ip_address?`<span class="mono">${esc(r.ip_address)}</span>`:dash],
            ['User Agent', r.user_agent?`<span class="small dim">${esc(r.user_agent)}</span>`:dash],
          ]))}
          ${inspSection('State Change','git', kv([
            ['Previous State', text(r.prev_value)],
            ['New State', text(r.new_value)],
          ]))}
          ${r.detail ? inspSection('Detail','info', `<div class="quote">${esc(r.detail)}</div>`) : ''}
          ${Object.keys(r.event_metadata || {}).length ? inspSection('Metadata','settings',
            `<div class="quote" style="font-family:Consolas,monospace;font-size:11px;white-space:pre-wrap">${esc(JSON.stringify(r.event_metadata, null, 2))}</div>`) : ''}
          ${inspSection('Integrity','shieldCheck', kv([
            ['Event Hash', r.checksum?`<span class="mono small">sha256:${esc(String(r.checksum).slice(0,16))}…</span>`:dash],
          ]))}`;
        const close = insp.querySelector('#apAudClose');
        if(close) close.addEventListener('click', paintAuditInspector);
      }

      /* ---- decisions ---- */
      function decide(r, verb){
        if(!allowed('approver','Deciding an approval request')) return;
        const titles = { approve:'Approve Request', reject:'Reject Request', escalate:'Escalate Request' };
        const icons = { approve:'checkCircle', reject:'xCircle', escalate:'users' };
        openModal({
          title: titles[verb] + ' — ' + r.request_ref, icon: icons[verb],
          body:`<p style="margin-top:0"><b style="color:var(--text)">${esc(r.action)}</b>${r.resource?' on '+esc(r.resource):''}
              — raised ${esc(rel(r.requested_at))}${r.requested_by_name?' by '+esc(r.requested_by_name):''}.</p>
            <p class="small muted">${verb === 'approve'
              ? 'Approving returns the replayable action to the caller and writes an immutable audit event.'
              : verb === 'reject'
                ? 'A rejection note is required — it is quoted verbatim in the audit trail.'
                : 'The request stays open with its original SLA and moves to the reviewer you name.'}</p>
            ${verb === 'escalate' ? `<div class="form-row"><label>ESCALATE TO</label>
              <select class="filter-select w-100" id="apEscTo" style="height:34px"><option value="">Loading members…</option></select></div>` : ''}
            <div class="form-row"><label>${verb === 'reject' ? 'REASON (REQUIRED)' : 'NOTE'}</label>
              <textarea class="input" id="apNote" rows="3" placeholder="${verb === 'reject' ? 'Why is this being refused?' : 'Anything the record should carry…'}"></textarea></div>`,
          footer:[{label:'Cancel'},{label: titles[verb].split(' ')[0], cls: verb === 'reject' ? 'danger' : 'primary', onClick: async (close, modal)=>{
            const note = modal.querySelector('#apNote').value.trim();
            if(verb === 'reject' && !note){ toast('error','A reason is required','A rejection is recorded with the reason it was refused.'); return; }
            const body = note ? { note } : {};
            if(verb === 'escalate'){
              const to = modal.querySelector('#apEscTo').value;
              if(to) body.escalate_to_user_id = to;
            }
            close();
            try {
              const res = await Store.mutate(()=>API.approvals[verb](r.id, body), { event:'approvals:changed' });
              toast(verb === 'approve' ? 'success' : verb === 'reject' ? 'error' : 'warn',
                `Request ${res.request.status.toLowerCase()}`, res.message);
              if(res.follow_on) openModal({ title:'Follow-on Action', icon:'zap',
                body:`<p style="margin-top:0">The approved payload names an action for the caller to carry out. The control plane does not execute it.</p>`
                  + kv([['Action', esc(res.follow_on.action)], ['Target', esc(res.follow_on.target || '—')]])
                  + `<div class="quote" style="font-family:Consolas,monospace;font-size:11px;white-space:pre-wrap">${esc(JSON.stringify(res.follow_on.parameters, null, 2))}</div>`,
                footer:[{label:'Close'}] });
              table.refresh();
              loadSummary();
              Store.refreshBadges();
              if(currentId === r.id) showRequest(res.request);
            } catch (err) {
              toast('error','Could not record the decision', err.message);
            }
          }}],
          onOpen(modal){
            if(verb !== 'escalate') return;
            const select = modal.querySelector('#apEscTo');
            API.approvals.approvers({ page_size: 100, sort:'full_name' })
              .then(page => {
                const members = page.items || [];
                select.innerHTML = `<option value="">Leave with the current reviewers</option>` + members.map(m=>
                  `<option value="${esc(m.user_id || m.id)}">${esc(m.full_name || m.email)}${m.role?' — '+esc(m.role):''}</option>`).join('');
              })
              .catch(err => { select.innerHTML = `<option value="">Members unavailable — ${esc(err.message)}</option>`; });
          },
        });
      }

      function addComment(r){
        if(!allowed('member','Commenting on a request')) return;
        openModal({
          title:'Add Comment — '+r.request_ref, icon:'chat',
          body:`<textarea class="input" id="apCmt" rows="3" placeholder="Add a review comment…"></textarea>`,
          footer:[{label:'Cancel'},{label:'Add Comment', cls:'primary', onClick: async (close, modal)=>{
            const body = modal.querySelector('#apCmt').value.trim();
            if(!body){ toast('error','Nothing to add','Write a comment before saving it to the record.'); return; }
            close();
            try {
              await Store.mutate(()=>API.approvals.comment(r.id, { body }), { event:'approvals:changed' });
              toast('success','Comment added','Recorded on the request and in the audit trail.');
              showRequest(r);
              table.refresh();
            } catch (err) { toast('error','Could not add the comment', err.message); }
          }}],
        });
      }

      /* ---- inspector: the decision card ---- */
      function showRequest(row){
        const insp = document.getElementById('apInspector');
        if(!insp || !row) return;
        currentId = row.id;
        document.getElementById('apLayout').classList.remove('collapsed');
        insp.innerHTML = `<div class="insp-head"><div class="grow">
            <div class="flex between"><span class="mono" style="font-weight:700">${esc(row.request_ref)}</span>${badge(row.status)}</div></div>
            <button class="icon-btn insp-close" id="apInspClose">${ICONS.x}</button></div>
          <div class="card-loading" style="height:240px;margin:12px"></div>`;
        insp.querySelector('#apInspClose').addEventListener('click', ()=>document.getElementById('apLayout').classList.add('collapsed'));

        Promise.all([
          API.approvals.get(row.id),
          API.approvals.comments(row.id).catch(()=>[]),
        ])
          .then(([r, comments]) => { if(currentId === row.id) paintRequest(insp, r, comments || []); })
          .catch(err => {
            if(currentId !== row.id) return;
            const holder = insp.querySelector('.card-loading');
            if(holder) holder.replaceWith(screenError(err, ()=>showRequest(row), 'this request'));
          });
      }

      function paintRequest(insp, r, comments){
        const impact = r.impact || {};
        const extraImpact = Object.keys(impact).filter(k=>
          ['financial','systems','sensitivity','customers'].indexOf(k) < 0 && impact[k] != null);
        const nextStep = (r.workflow || []).findIndex(w=>!w.done);
        insp.innerHTML = `
          <div class="insp-head"><div class="grow">
            <div class="flex between"><span class="mono" style="font-weight:700">${esc(r.request_ref)}</span>${badge(r.status)}</div>
            <div class="insp-title" style="margin-top:8px;display:flex;gap:8px;align-items:center">
              <span class="entity-ico" style="background:var(--red-dim);color:#B91C1C;width:26px;height:26px">${ICONS.shield}</span>
              ${esc(r.policy_name || r.action)}</div>
            <div class="insp-sub">${esc(r.reason || r.action_detail || '')}</div></div>
            <button class="icon-btn insp-close" id="apInspClose">${ICONS.x}</button></div>
          ${inspSection('Request Summary','fileText', kv([
            ['Action', `<b>${esc(r.action)}</b>`],
            ['Description', r.action_detail?`<span class="small dim right">${esc(r.action_detail)}</span>`:dash],
            ['Resource', text(r.resource)],
            ['Agent', r.agent_id
              ? `<span class="link" data-nav="agent/${esc(r.agent_id)}">${esc(r.agent_name || '—')}</span>${r.agent_platform?'<br><span class="faint small">('+esc(r.agent_platform)+')</span>':''}`
              : text(r.agent_name)],
            ['Source', text(r.source)],
            ['Requested By', r.requested_by_name ? esc(r.requested_by_name) + (r.requested_by_team?' ('+esc(r.requested_by_team)+')':'') : dash],
            ['Requested', `${when(r.requested_at)} (${esc(rel(r.requested_at))})`],
            ['Risk Level', r.risk?riskBadge(r.risk):dash],
            ['Policy', text(r.policy_name)],
            r.is_open
              ? ['SLA', r.sla_due_at
                  ? `${slaCell(r)} <span class="faint small">due ${esc(when(r.sla_due_at))}</span>`
                  : dash]
              : ['Decided By', r.decided_by_name ? esc(r.decided_by_name) + ' · ' + esc(rel(r.decided_at)) : dash],
            ...(r.escalated_to_name ? [['Escalated To', esc(r.escalated_to_name)]] : []),
            ...(r.decision_note ? [['Decision Note', `<span class="small dim right">${esc(r.decision_note)}</span>`]] : []),
          ]))}
          ${inspSection('Impact Analysis','target', `<div class="grid g4" style="gap:7px;text-align:center">
            ${[['Financial Impact', impact.financial == null ? '—' : impact.financial, 'dollar'],
               ['Affected Systems', impact.systems == null ? '—' : fmtFull(impact.systems), 'server'],
               ['Data Sensitivity', impact.sensitivity == null ? '—' : impact.sensitivity, 'lock'],
               ['Customers Impacted', impact.customers == null ? '—' : fmtFull(impact.customers), 'users']].map(x=>
              `<div style="background:var(--panel-2);border:1px solid var(--border-soft);border-radius:9px;padding:8px 3px">
                <span style="width:13px;display:inline-flex;color:var(--purple-bright)">${ICONS[x[2]]}</span>
                <div style="font-size:12.5px;font-weight:700;margin-top:3px">${esc(String(x[1]))}</div>
                <div class="small faint" style="font-size:9.5px">${x[0]}</div></div>`).join('')}</div>
            ${extraImpact.length ? kv(extraImpact.map(k=>[k.replace(/_/g,' '), esc(String(impact[k]))])) : ''}`)}
          ${inspSection('Approval Workflow','git', (r.workflow||[]).length
            ? `<div class="pipe">${r.workflow.map((w,wi)=>`
              <div class="pipe-step"><div class="pipe-dot ${w.done?'done':(wi===nextStep?'active':'')}">${w.done?ICONS.check:wi+1}</div>
              <div class="pipe-body"><div class="pipe-title"><span>${esc(w.step)}</span></div>
              <div class="pipe-sub">${w.done ? (w.by?esc(w.by)+' · ':'') + (w.ts?esc(fmtTime(ts(w.ts))):'Completed') : 'Waiting'}</div></div></div>`).join('')}</div>`
            : '<span class="faint small">No workflow recorded on this request.</span>')}
          ${inspSection('Comments ('+comments.length+')','chat', comments.length
            ? comments.map(cm=>`<div class="quote"><b>${esc(cm.author_name || 'Unknown')}</b> · <span class="faint small">${esc(rel(cm.created_at))}</span><br>${esc(cm.body)}</div>`).join('')
            : '<span class="faint small">No comments on this request yet.</span>')}
          ${r.is_open ? `
          <div class="insp-section"><div class="insp-section-title">Review Actions</div>
            <div class="grid g2" style="gap:8px">
              <button class="btn success block" id="apApprove">${ICONS.checkCircle}Approve</button>
              <button class="btn danger block" id="apReject">${ICONS.xCircle}Reject</button>
            </div>
            <button class="btn block" id="apEscalate" style="margin-top:8px">${ICONS.users}Escalate</button>
            <button class="btn block" id="apComment" style="margin-top:8px">${ICONS.chat}Add Comment</button>
          </div>` : `
          <div class="insp-section"><button class="btn sm block" id="apComment">${ICONS.chat}Add Comment</button></div>`}`;

        insp.querySelector('#apInspClose').addEventListener('click', ()=>document.getElementById('apLayout').classList.add('collapsed'));
        if(r.is_open){
          requireRole(insp.querySelector('#apApprove'), 'approver', 'Approving a request')
            .addEventListener('click', ()=>decide(r,'approve'));
          requireRole(insp.querySelector('#apReject'), 'approver', 'Rejecting a request')
            .addEventListener('click', ()=>decide(r,'reject'));
          requireRole(insp.querySelector('#apEscalate'), 'approver', 'Escalating a request')
            .addEventListener('click', ()=>decide(r,'escalate'));
        }
        requireRole(insp.querySelector('#apComment'), 'member', 'Commenting on a request')
          .addEventListener('click', ()=>addComment(r));
      }

      /* ---- approval rules ---- */
      document.getElementById('apNewRule').addEventListener('click', ()=>{
        if(!allowed('admin','Creating an approval rule')) return;
        openModal({
          title:'New Approval Rule', icon:'stamp',
          body:`<div class="form-row"><label>RULE NAME</label><input class="input" id="arName" placeholder="e.g. Payments above $5,000"></div>
            <div class="grid g2">
              <div class="form-row"><label>TRIGGER</label><select class="filter-select w-100" id="arTrigger" style="height:34px">${optionList(APPROVAL_TRIGGERS)}</select></div>
              <div class="form-row"><label>SLA</label><select class="filter-select w-100" id="arSla" style="height:34px">
                <option value="15">15 minutes</option><option value="60">1 hour</option>
                <option value="240" selected>4 hours</option><option value="1440">24 hours</option></select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>RISK LEVEL</label><select class="filter-select w-100" id="arRisk" style="height:34px">${optionList(RISKS,'Medium')}</select></div>
              <div class="form-row"><label>THRESHOLD AMOUNT</label><input class="input" id="arThreshold" type="number" min="0" placeholder="e.g. 5000"></div>
            </div>
            <div class="form-row"><label>APPROVERS (SELECT ONE OR MORE)</label>
              <select class="filter-select w-100" id="arApprovers" multiple size="5" style="height:auto"><option>Loading members…</option></select></div>
            <div class="form-row"><label>DESCRIPTION</label><textarea class="input" id="arDesc" rows="2" placeholder="What does this rule stop?"></textarea></div>`,
          footer:[{label:'Cancel'},{label:'Create Rule', cls:'primary', onClick: async (close, modal)=>{
            const name = modal.querySelector('#arName').value.trim();
            if(name.length < 2){ toast('error','Name required','An approval rule needs a name.'); return; }
            const approvers = Array.from(modal.querySelector('#arApprovers').selectedOptions).map(o=>o.value).filter(Boolean);
            if(!approvers.length){ toast('error','Approvers required','Name at least one approver group or person.'); return; }
            const threshold = modal.querySelector('#arThreshold').value;
            const body = {
              name,
              description: modal.querySelector('#arDesc').value.trim() || null,
              trigger: modal.querySelector('#arTrigger').value,
              approvers,
              sla_minutes: parseInt(modal.querySelector('#arSla').value, 10),
              risk_level: modal.querySelector('#arRisk').value,
            };
            if(threshold !== '') body.threshold_amount = Number(threshold);
            close();
            try {
              const rule = await Store.mutate(()=>API.approvals.rules.create(body), { event:'approvals:changed' });
              toast('success','Approval rule created', `${rule.name} routes to ${rule.approvers.join(', ')} within ${rule.sla_minutes} minutes.`);
              loadSummary();
            } catch (err) { toast('error','Could not create the rule', err.message); }
          }}],
          onOpen(modal){
            const select = modal.querySelector('#arApprovers');
            API.approvals.approvers({ page_size: 100, sort:'full_name' })
              .then(page => {
                const members = page.items || [];
                select.innerHTML = members.length
                  ? members.map(m=>`<option value="${esc(m.full_name || m.email)}">${esc(m.full_name || m.email)}${m.role?' — '+esc(m.role):''}</option>`).join('')
                  : '<option value="">No workspace members found</option>';
              })
              .catch(err => { select.innerHTML = `<option value="">Members unavailable — ${esc(err.message)}</option>`; });
          },
        });
      });

      loadSummary();
    },
  };
})();
