/* FD AI Command Center — CONFIGURATION screens.
 *
 *   Configuration Center · Prompt Manager · RAG & Knowledge Governance ·
 *   Secrets & Credentials
 *
 * These four screens decide what an agent is made of, so nothing here is
 * approximated. Every number is what the server returned for this
 * workspace: a field the server did not send renders as a dash, a call that
 * failed says so and offers itself again, and an empty table says the
 * workspace is empty rather than filling itself in. Progress bars follow real
 * jobs — the knowledge sync bar is polled from the server's own job state,
 * never from a timer.
 */
(function(){
  'use strict';
  window.SCREENS = window.SCREENS || {};
  const { esc, relTime, fmtDate, fmtDateTime, fmtFull, fmtNum, gaugeRing, barPct, hbars, donut } = U;
  const { badge, statusText, riskBadge, ownerCell, entityCell, kpiRow, kpiSkeleton, dataTable,
          pageHead, searchBox, tabBar, inspSection, kv, toast, openModal, confirmModal, screenError } = C;

  /* ---------------------------------------------------------------- values */

  const dash = '<span class="faint">—</span>';
  const ts = (v) => v ? new Date(v).getTime() : null;
  const num = (v) => v == null ? dash : fmtFull(v);
  const compact = (v) => v == null ? dash : fmtNum(v);
  const pct = (v, digits) => v == null ? dash : Number(v).toFixed(digits == null ? 1 : digits) + '%';
  const score = (v, digits) => v == null ? dash : Number(v).toFixed(digits == null ? 2 : digits);
  const text = (v) => (v == null || v === '') ? dash : esc(v);
  const dim = (v) => (v == null || v === '') ? dash : `<span class="dim">${esc(v)}</span>`;
  const mono = (v) => (v == null || v === '') ? dash : `<span class="mono">${esc(v)}</span>`;
  const rel = (v) => v ? relTime(ts(v)) : dash;
  const when = (v) => v ? fmtDateTime(ts(v)) : dash;
  const day = (v) => v ? fmtDate(ts(v)) : dash;

  /**
   * "in 3 days" for a moment still ahead; null once it has passed. relTime only
   * looks back — it calls every future timestamp "just now".
   */
  function until(v){
    const left = v ? ts(v) - Date.now() : null;
    if(left == null || isNaN(left) || left <= 0) return null;
    if(left < 3600000) return 'in under an hour';
    if(left < 86400000){ const h = Math.round(left / 3600000); return `in ${h} hour${h === 1 ? '' : 's'}`; }
    const d = Math.round(left / 86400000);
    return `in ${d} day${d === 1 ? '' : 's'}`;
  }

  /** Up/down only when the server actually reported a movement. */
  function deltaDir(v){ return v == null || v === 0 ? null : (v > 0 ? 'up' : 'down'); }

  function clip(s, n){
    const v = String(s == null ? '' : s);
    return v.length > n ? v.slice(0, n) + '…' : v;
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
  function allowed(role, what){
    if(Store.session.can(role)) return true;
    toast('error','Not permitted', `${what || 'This action'} requires the ${role} role.`);
    return false;
  }
  /** A row-action menu entry that explains itself instead of failing. */
  function roleItem(role, what, item){
    if(Store.session.can(role)) return item;
    return Object.assign({}, item, {
      label: item.label + ' (needs ' + role + ')',
      onClick: () => toast('error','Not permitted', `${what} requires the ${role} role.`),
    });
  }

  /* -------------------------------------------------------------- fragments */

  const EMPTY = (icon, title, body) =>
    `<div class="empty-state">${ICONS[icon] || ICONS.search}<div class="es-title">${esc(title)}</div><div>${esc(body || '')}</div></div>`;

  const LOADING = (h) => `<div class="card-loading" style="height:${h || 140}px"></div>`;

  function optionList(values, selected){
    return values.map(v=>`<option ${v===selected?'selected':''}>${esc(v)}</option>`).join('');
  }

  /**
   * optionList for a field the record may hold empty, or hold with a value this
   * list does not carry. Without an option of its own such a value selects
   * nothing, the browser settles on the first entry, and Save writes that entry
   * back — a secret stored with no environment became "Production" the first
   * time anyone edited its risk.
   */
  function optionListKeeping(values, selected){
    const own = (selected == null || selected === '') ? '<option value="" selected>—</option>'
      : values.indexOf(selected) < 0 ? `<option selected>${esc(selected)}</option>` : '';
    return own + optionList(values, selected);
  }

  /**
   * Fill one of a table's filter dropdowns with values the server told us
   * exist. `pairs` may be plain strings or {value,label} — the second form is
   * what the owner filters need, because the server matches a user id while
   * the reader picks a name.
   */
  function fillOptions(table, index, pairs){
    const sel = table.filterEl && table.filterEl.querySelector(`[data-fi="${index}"]`);
    if(!sel) return;
    pairs.forEach(p=>{
      const value = p && p.value != null ? p.value : p;
      const label = p && p.label != null ? p.label : p;
      if(value == null || value === '') return;
      if(Array.from(sel.options).some(o=>o.value === String(value))) return;
      const opt = document.createElement('option');
      opt.value = String(value);
      opt.textContent = String(label);
      sel.appendChild(opt);
    });
  }

  /** Drive a table's dropdown from a tab click so the query still goes server-side. */
  function setFilter(table, index, value){
    const sel = table.filterEl && table.filterEl.querySelector(`[data-fi="${index}"]`);
    if(!sel) return;
    sel.value = value || '';
    sel.dispatchEvent(new Event('change'));
  }

  /** Replace a container with the failure and a retry that re-runs the call. */
  function fail(host, err, retry, what){
    if(!host) return;
    host.innerHTML = '';
    host.appendChild(screenError(err, retry, what));
  }

  /**
   * What a failed call should say. A request the schema rejects answers with
   * one fixed headline — "One or more fields are invalid." — and puts the real
   * reasons in `details.fields`, so the field messages win when there are any.
   * A rule that spans the whole body is filed under "body" and prefixed by the
   * validator; neither helps the reader, so both are dropped.
   */
  function msgOf(err){
    const fields = (err && err.details && err.details.fields) || [];
    if(fields.length){
      return fields.map(f => (f.field && f.field !== 'body' ? f.field + ': ' : '') +
        String(f.message || '').replace(/^Value error,\s*/, '')).join(' · ');
    }
    return (err && err.message) || 'The request failed.';
  }

  const ENVIRONMENTS = ['Production','Staging','UAT','Development','QA','Sandbox','DR'];

  /**
   * The member list behind one screen's owner pickers and its owner filter.
   *
   * The pickers used to be built from whatever had arrived by the time a dialog
   * opened. When nothing had — the call failed, or was still in flight — the
   * select held a single empty option and Save sent that emptiness as
   * `owner_user_id: null`: an operator correcting a description cleared the
   * owner without a word. The same happened to an owner the list did not
   * carry (the directory names active members only), because the select fell
   * back to its first option, "Unassigned".
   *
   * So a picker now keeps three promises. The record's current owner is always
   * one of its options, listed or not. A picker whose list is missing is
   * disabled, asks for the list again and says so if that fails too. And
   * `change()` reports an owner only when the reader picked a different one —
   * a picker that never loaded reports nothing, so nothing is sent.
   */
  function ownerDirectory(fetchOwners){
    let items = [], ready = false, pending = null;

    function load(){
      if(ready) return Promise.resolve(items);
      if(!pending){
        pending = fetchOwners()
          .then(page => {
            items = Array.isArray(page) ? page : ((page && page.items) || []);
            ready = true;
            return items;
          })
          .finally(() => { pending = null; });
      }
      return pending;
    }

    /* `required` leaves "Unassigned" out, for a dialog whose server would not
       honour it — a secret stored with no owner is given to its creator. */
    function options(selectedId, selectedName, required){
      const unlisted = selectedId && !items.some(u => u.id === selectedId);
      const keep = unlisted
        ? `<option value="${esc(selectedId)}" selected>${esc(selectedName || 'Current owner')}</option>` : '';
      if(!ready) return keep || '<option value="">Members not loaded</option>';
      return (required ? '' : '<option value="">Unassigned</option>') + keep + items.map(u =>
        `<option value="${esc(u.id)}" ${u.id === selectedId ? 'selected' : ''}>${esc(u.full_name || u.email || u.id)}</option>`).join('');
    }

    /** Paint a dialog's owner select, and fetch the list again if it is missing. */
    function attach(select, selectedId, selectedName, required){
      if(!select) return;
      const paint = () => {
        select.innerHTML = options(selectedId, selectedName, required);
        select.disabled = !ready;
        select.title = ready ? '' : 'The member list has not loaded, so the owner stays as it is.';
      };
      paint();
      if(ready) return;
      load()
        .then(() => { if(select.isConnected) paint(); })
        .catch(err => {
          if(select.isConnected) toast('warn','Owner list unavailable', 'The owner stays as it is — ' + msgOf(err));
        });
    }

    /**
     * The owner to send, or `undefined` when there is nothing to send: the
     * picker never loaded, or it still shows the owner the record already has.
     * `null` is a deliberate "Unassigned".
     */
    function change(select, currentId){
      if(!select || select.disabled || !ready) return undefined;
      const picked = select.value || null;
      return picked === (currentId || null) ? undefined : picked;
    }

    return { load, options, attach, change };
  }

  /** The signed-in person, as the default owner of something they are creating. */
  function me(){ return Store.session.user || {}; }

  /* ======================== CONFIGURATION CENTER ======================== */

  const CFG_TYPES = ['Model','Prompt','Tool','Guardrail','Routing','Quota','Environment','Connector','MCP Server'];
  const CFG_STATUSES = ['Active','Draft','Deprecated','Archived'];
  const IMPACTS = ['Low','Medium','High','Critical'];
  const TYPE_COLORS = {Model:'blue',Prompt:'purple',Tool:'cyan',Guardrail:'green',Routing:'pink',
    Quota:'amber',Environment:'blue',Connector:'green','MCP Server':'gray'};
  const TYPE_ICONS = {Model:'cpu',Prompt:'pen',Tool:'tool',Guardrail:'shieldCheck',Routing:'git',
    Quota:'gauge',Environment:'layers',Connector:'link','MCP Server':'server'};
  const CFG_KPIS = ['Total Configurations','Active','Draft','Deprecated','Archived','Config Changes (30d)'];
  const CFG_TABS = [
    ['All Configurations', null], ['Models','Model'], ['Prompts','Prompt'], ['Tools','Tool'],
    ['Guardrails','Guardrail'], ['Routing','Routing'], ['Quotas','Quota'],
    ['Environments','Environment'], ['Connectors','Connector'], ['MCP Servers','MCP Server'],
  ];

  function cfgStatusColor(s){
    return s === 'Active' ? 'green' : s === 'Draft' ? 'blue' : s === 'Deprecated' ? 'amber' : 'gray';
  }

  SCREENS['configurations'] = {
    title:'Configuration Center',
    render(main){
      let selectedId = null;
      const owners = ownerDirectory(() => API.configurations.owners());

      main.innerHTML = `
        ${pageHead({title:'Configuration Center', sub:'Manage and version all configuration assets including models, prompts, tools, guardrails, routing, and quotas.',
          actions:`${searchBox('cfSearch','Search configurations…')}
          <button class="btn" id="cfImport">${ICONS.upload}Import</button>
          <button class="btn" id="cfExport">${ICONS.download}Export</button>
          <button class="btn primary" id="cfNew">${ICONS.plus}New Configuration</button>`})}
        <div id="cfKpis">${kpiSkeleton(CFG_KPIS)}</div>
        <div id="cfTabs"></div>
        <div class="with-inspector" id="cfLayout">
          <div id="cfTableWrap"></div>
          <div class="inspector" id="cfInspector">
            <div class="insp-head"><div><div class="insp-title">Configuration</div>
              <div class="insp-sub">Select a row to inspect it.</div></div></div>
          </div>
        </div>`;

      requireRole(document.getElementById('cfNew'), 'operator', 'Creating a configuration');
      requireRole(document.getElementById('cfImport'), 'admin', 'Importing configurations');

      /* ---- KPI cards, all six off /configurations/summary ---- */
      function loadSummary(){
        const host = document.getElementById('cfKpis');
        if(!host) return;
        host.innerHTML = kpiSkeleton(CFG_KPIS);
        API.configurations.summary()
          .then(s => {
            if(!document.getElementById('cfKpis')) return;
            const slice = (status) => (s.status_breakdown || []).find(b => b.status === status);
            const sub = (status) => {
              const b = slice(status);
              return b && b.percent != null ? pct(b.percent, 0) + ' of total' : null;
            };
            host.innerHTML = kpiRow([
              {label:'Total Configurations', value:num(s.total), icon:'settings', color:'purple',
                delta: s.created_30d == null ? null : String(s.created_30d),
                dir: deltaDir(s.created_30d), good: true,
                vs: `created in the last ${s.window_days || 30} days`},
              {label:'Active', value:`<span class="st-green">${num(s.active)}</span>`, sub:sub('Active'), icon:'checkCircle', color:'green'},
              {label:'Draft', value:`<span class="st-blue">${num(s.draft)}</span>`, sub:sub('Draft'), icon:'edit', color:'blue'},
              {label:'Deprecated', value:`<span class="st-amber">${num(s.deprecated)}</span>`, sub:sub('Deprecated'), icon:'clock', color:'amber'},
              {label:'Archived', value:num(s.archived), sub:sub('Archived'), icon:'folder', color:'gray'},
              {label:`Config Changes (${s.window_days || 30}d)`, value:num(s.changes_30d), icon:'git', color:'orange',
                sub: s.versions_30d == null ? null : `${fmtFull(s.versions_30d)} versions published`},
            ]);
          })
          .catch(err => fail(host, err, loadSummary, 'the configuration summary'));
      }
      loadSummary();

      /* ---- the table: every filter, sort and page is a server query ---- */
      const table = dataTable({
        rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'configurations',
        searchPlaceholder:'Search by name, description, owner…',
        defaultSort:{ key:'updated_at', dir:-1 },
        emptyText:'No configurations in this workspace yet',
        columns:[
          {key:'name', label:'Name', render:r=>entityCell(r.name, clip(r.description || '', 44),
            TYPE_ICONS[r.config_type] || 'settings', TYPE_COLORS[r.config_type] || 'gray')},
          {key:'config_type', label:'Type', render:r=>r.config_type?badge(r.config_type, TYPE_COLORS[r.config_type]||'gray'):dash},
          {key:'environment', label:'Environment', render:r=>r.environment?badge(r.environment):dash},
          {key:'status', label:'Status', render:r=>r.status?statusText(r.status, cfgStatusColor(r.status)):dash},
          {key:'current_version', label:'Version', render:r=>mono(r.current_version)},
          {key:'updated_at', label:'Last Modified', render:r=>`<span class="dim nowrap">${when(r.updated_at)}</span>`},
          {key:'owner', label:'Owner', render:r=>r.owner_name?ownerCell(r.owner_name, r.owner_team||''):dash},
          {key:'impact', label:'Impact', render:r=>r.impact?riskBadge(r.impact):dash},
        ],
        filters:[
          {key:'type', label:'Type', param:'type', options:CFG_TYPES, allLabel:'All Types'},
          {key:'status', label:'Status', param:'status', options:CFG_STATUSES, allLabel:'All Status'},
          {key:'env', label:'Environment', param:'env', options:ENVIRONMENTS, allLabel:'All Environments'},
          {key:'impact', label:'Impact', param:'impact', options:IMPACTS, allLabel:'All Impact Levels'},
          {key:'owner', label:'Owner', param:'owner', options:[], allLabel:'All Owners'},
        ],
        source: (params) => API.configurations.list(params),
        exportSource: (params) => API.configurations.export(params),
        autoSelectFirst: true,
        onSelect: showCfg,
        rowActions: r=>[
          {label:'Create New Version', icon:'git', onClick:()=>newVersion(r)},
          {label:'Edit', icon:'edit', onClick:()=>editCfg(r)},
          {label:'Clone', icon:'copy', onClick:()=>cloneCfg(r)},
          {label:'Validate', icon:'shieldCheck', onClick:()=>validateCfg(r)},
          {label:'View History', icon:'history', onClick:()=>{ showCfg(r); openTab(1); }},
          {sep:true},
          (r.status === 'Deprecated' || r.status === 'Archived')
            ? roleItem('admin','Restoring a configuration', {label:'Restore', icon:'refresh', onClick:()=>restoreCfg(r)})
            : roleItem('admin','Deprecating a configuration', {label:'Deprecate', icon:'clock', danger:true, onClick:()=>deprecateCfg(r)}),
        ],
      });

      const wrap = document.getElementById('cfTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);
      document.getElementById('cfSearch').addEventListener('input', e=>table.search(e.target.value));
      document.getElementById('cfExport').addEventListener('click', ()=>table.export());

      // The owner dropdown lists this workspace's members; the server matches ids.
      // The names-only directory opens at operator, which is also the lowest
      // role that can change an owner, so nobody below it is sent to collect a
      // 403. A failure here leaves the filter at "All Owners"; the dialogs ask
      // again when they open and say so if the list still will not come.
      if(Store.session.can('operator')){
        owners.load()
          .then(list => fillOptions(table, 4, list.map(u=>({ value:u.id, label:u.full_name || u.email || u.id }))))
          .catch(()=>{});
      }

      tabBar(document.getElementById('cfTabs'), CFG_TABS.map(t=>({label:t[0]})),
        i => setFilter(table, 0, CFG_TABS[i][1]));

      function refreshAll(){ table.refresh(); loadSummary(); }

      /* ------------------------------ mutations ------------------------------ */

      function parsePayload(modal, sel){
        const raw = (modal.querySelector(sel).value || '').trim();
        if(!raw) return {};
        try { return JSON.parse(raw); }
        catch (e) { throw new Error('The body is not valid JSON: ' + e.message); }
      }

      function newCfg(){
        if(!allowed('operator','Creating a configuration')) return;
        openModal({
          title:'New Configuration', icon:'settings', wide:true,
          body:`<div class="form-row"><label>NAME</label><input class="input" id="ncName" placeholder="e.g. Claims Triage Model"></div>
            <div class="grid g2">
              <div class="form-row"><label>TYPE</label><select class="filter-select w-100" id="ncType" style="height:34px">${optionList(CFG_TYPES,'Model')}</select></div>
              <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="ncEnv" style="height:34px">${optionList(ENVIRONMENTS,'Development')}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>IMPACT</label><select class="filter-select w-100" id="ncImpact" style="height:34px">${optionList(IMPACTS,'Low')}</select></div>
              <div class="form-row"><label>OWNER</label><select class="filter-select w-100" id="ncOwner" style="height:34px">${owners.options(me().id, me().full_name)}</select></div>
            </div>
            <div class="form-row"><label>DESCRIPTION</label><input class="input" id="ncDesc" placeholder="What does this configuration control?"></div>
            <div class="grid g2">
              <div class="form-row"><label>VERSION</label><input class="input" id="ncVer" value="v0.1.0"></div>
              <div class="form-row"><label>CHANGE NOTE</label><input class="input" id="ncNote" placeholder="Initial revision"></div>
            </div>
            <div class="form-row"><label>BODY (JSON)</label><textarea class="input" id="ncBody" rows="6">{}</textarea></div>
            <div id="ncSchema" class="small faint">Loading the declared fields for this type…</div>`,
          footer:[
            {label:'Cancel'},
            {label:'Create Draft', cls:'primary', onClick: async (close, modal) => {
              const name = (modal.querySelector('#ncName').value || '').trim();
              if(!name){ toast('error','Name required','Give the configuration a name.'); return; }
              let payload;
              try { payload = parsePayload(modal, '#ncBody'); }
              catch (err) { toast('error','Invalid body', err.message); return; }
              const body = {
                name,
                config_type: modal.querySelector('#ncType').value,
                environment: modal.querySelector('#ncEnv').value,
                impact: modal.querySelector('#ncImpact').value,
                description: (modal.querySelector('#ncDesc').value || '').trim() || null,
                owner_user_id: modal.querySelector('#ncOwner').value || null,
                version: (modal.querySelector('#ncVer').value || '').trim() || 'v0.1.0',
                payload,
                change_note: (modal.querySelector('#ncNote').value || '').trim() || null,
              };
              try {
                const created = await Store.mutate(() => API.configurations.create(body), { event:'configurations:changed' });
                close();
                toast('success','Configuration created', `${created.name} ${created.current_version || ''} — validate before activating.`);
                refreshAll();
              } catch (err) {
                toast('error','Could not create the configuration', msgOf(err));
              }
            }},
          ],
          onOpen(modal){
            owners.attach(modal.querySelector('#ncOwner'), me().id, me().full_name);
            const hint = modal.querySelector('#ncSchema');
            const typeSel = modal.querySelector('#ncType');
            function loadSchema(){
              hint.textContent = 'Loading the declared fields for this type…';
              API.configurations.schema({ type: typeSel.value })
                .then(list => {
                  const spec = (list || [])[0];
                  if(!spec || !(spec.fields || []).length){
                    hint.textContent = 'The server declares no required fields for this type.';
                    return;
                  }
                  hint.innerHTML = 'Declared fields: ' + spec.fields.map(f=>
                    `<span class="mono">${esc(f.name)}</span>${f.required?' <span class="st-red">*</span>':''}`).join(', ');
                })
                .catch(err => { hint.textContent = 'Could not load the field contract: ' + msgOf(err); });
            }
            typeSel.addEventListener('change', loadSchema);
            loadSchema();
          },
        });
      }
      document.getElementById('cfNew').addEventListener('click', newCfg);

      function editCfg(r){
        if(!allowed('operator','Editing a configuration')) return;
        openModal({
          title:'Edit Configuration — ' + r.name, icon:'edit',
          body:`<div class="form-row"><label>NAME</label><input class="input" id="ecName" value="${esc(r.name || '')}"></div>
            <div class="grid g2">
              <div class="form-row"><label>TYPE</label><select class="filter-select w-100" id="ecType" style="height:34px">${optionList(CFG_TYPES, r.config_type)}</select></div>
              <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="ecEnv" style="height:34px">${optionList(ENVIRONMENTS, r.environment)}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>IMPACT</label><select class="filter-select w-100" id="ecImpact" style="height:34px">${optionList(IMPACTS, r.impact)}</select></div>
              <div class="form-row"><label>OWNER</label><select class="filter-select w-100" id="ecOwner" style="height:34px">${owners.options(r.owner_user_id, r.owner_name)}</select></div>
            </div>
            <div class="form-row"><label>DESCRIPTION</label><input class="input" id="ecDesc" value="${esc(r.description || '')}"></div>
            <div class="quote small">${ICONS.info} Identity fields only. The body is changed by cutting a new version, so every change keeps a revision — but a new type or environment is re-validated against the live body, and a rename is refused while another configuration links to this one by name.</div>
            <div id="ecProblem"></div>`,
          footer:[
            {label:'Cancel'},
            {label:'Save Changes', cls:'primary', onClick: async (close, modal) => {
              const body = {
                name: (modal.querySelector('#ecName').value || '').trim(),
                config_type: modal.querySelector('#ecType').value,
                environment: modal.querySelector('#ecEnv').value,
                impact: modal.querySelector('#ecImpact').value,
                description: (modal.querySelector('#ecDesc').value || '').trim() || null,
                expected_updated_at: r.updated_at || null,
              };
              // The owner travels only when the reader chose a different one. A
              // picker that failed to load must never be read as "Unassigned".
              const owner = owners.change(modal.querySelector('#ecOwner'), r.owner_user_id);
              if(owner !== undefined) body.owner_user_id = owner;
              const problem = modal.querySelector('#ecProblem');
              problem.innerHTML = '';
              try {
                const saved = await Store.mutate(() => API.configurations.update(r.id, body), { event:'configurations:changed' });
                close();
                toast('success','Configuration updated', `${saved.name} saved.`);
                refreshAll();
                showCfg(saved);
              } catch (err) {
                toast('error','Could not save the configuration', msgOf(err));
                // Two refusals carry their reasons with them: the findings a
                // new type or environment raises against the live body, and the
                // configurations whose links a rename would break.
                const report = findingsOf(err);
                const dependents = err && err.status === 409 && err.details && err.details.dependents;
                if(report){
                  problem.innerHTML = `<div class="small st-red" style="margin:10px 0 6px">${esc(msgOf(err))}</div>` +
                    findingsTable(report.findings);
                } else if(Array.isArray(dependents) && dependents.length){
                  problem.innerHTML = `<div class="small st-red" style="margin:10px 0 6px">${esc(msgOf(err))}</div>` +
                    kv(dependents.map(d => [esc(d.name || d.configuration_id || '—'),
                      d.slot ? `links to it as <span class="mono">${esc(d.slot)}</span>` : dash]));
                }
              }
            }},
          ],
          onOpen(modal){ owners.attach(modal.querySelector('#ecOwner'), r.owner_user_id, r.owner_name); },
        });
      }

      function cloneCfg(r){
        if(!allowed('operator','Cloning a configuration')) return;
        openModal({
          title:'Clone Configuration — ' + r.name, icon:'copy',
          body:`<div class="form-row"><label>NEW NAME</label><input class="input" id="clName" value="${esc((r.name || '') + ' (Copy)')}"></div>
            <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="clEnv" style="height:34px">${optionList(ENVIRONMENTS, r.environment)}</select></div>
            <div class="quote small">${ICONS.info} The clone copies the live body into a fresh Draft. Nothing is activated.</div>`,
          footer:[
            {label:'Cancel'},
            {label:'Create Clone', cls:'primary', onClick: async (close, modal) => {
              const body = {
                name: (modal.querySelector('#clName').value || '').trim() || null,
                environment: modal.querySelector('#clEnv').value,
              };
              try {
                const created = await Store.mutate(() => API.configurations.clone(r.id, body), { event:'configurations:changed' });
                close();
                toast('success','Configuration cloned', `${created.name} created as ${created.status}.`);
                refreshAll();
              } catch (err) {
                toast('error','Could not clone', msgOf(err));
              }
            }},
          ],
        });
      }

      function newVersion(r){
        if(!allowed('operator','Cutting a new version')) return;
        openModal({
          title:'Create New Version — ' + r.name, icon:'git', wide:true,
          body:`<div class="grid g2">
              <div class="form-row"><label>CURRENT VERSION</label><input class="input" value="${esc(r.current_version || '—')}" disabled></div>
              <div class="form-row"><label>NEW VERSION</label><input class="input" id="nvVer" placeholder="e.g. v1.2.0"></div>
            </div>
            <div class="form-row"><label>CHANGE SUMMARY</label><textarea class="input" id="nvNote" rows="2" placeholder="What changed?"></textarea></div>
            <div class="form-row"><label>BODY (JSON)</label><textarea class="input" id="nvBody" rows="8">${LOADING(0)}</textarea></div>
            <label class="flex small" style="gap:7px;align-items:center"><input type="checkbox" id="nvActivate" checked> Activate this version once it validates</label>
            <div class="quote small">${ICONS.info} The server validates the body against the declared field contract for this type and returns per-field findings.</div>`,
          footer:[
            {label:'Cancel'},
            // Checks the body in the box and stores nothing. Without it the only
            // way to read the findings was to untick Activate and leave a Draft
            // behind for every attempt.
            {label:'Validate body', icon:'shieldCheck', close:false, onClick: async (close, modal) => {
              let payload;
              try { payload = parsePayload(modal, '#nvBody'); }
              catch (err) { toast('error','Invalid body', err.message); return; }
              const check = { payload };
              const label = (modal.querySelector('#nvVer').value || '').trim();
              if(label) check.version = label;
              try { showValidation(r, await API.configurations.validateBody(r.id, check)); }
              catch (err) { toast('error','Could not validate the body', msgOf(err)); }
            }},
            {label:'Create & Validate', cls:'primary', onClick: async (close, modal) => {
              const version = (modal.querySelector('#nvVer').value || '').trim();
              if(!version){ toast('error','Version required','Name the revision, e.g. v1.2.0.'); return; }
              let payload;
              try { payload = parsePayload(modal, '#nvBody'); }
              catch (err) { toast('error','Invalid body', err.message); return; }
              try {
                const res = await Store.mutate(() => API.configurations.newVersion(r.id, {
                  version, payload,
                  change_note: (modal.querySelector('#nvNote').value || '').trim() || null,
                  activate: modal.querySelector('#nvActivate').checked,
                }), { event:'configurations:changed' });
                close();
                const report = res.validation;
                if(report && report.valid === false){
                  toast('warn','Version created with findings', res.message || `${report.error_count} errors, ${report.warning_count} warnings.`);
                  showValidation(r, report);
                } else {
                  toast('success','Version created', res.message || `${r.name} ${version} published.`);
                }
                refreshAll();
                if(res.configuration) showCfg(res.configuration);
              } catch (err) {
                // Nothing was stored. The dialog stays open on the body that was
                // refused, with the fields at fault listed over it.
                toast('error','Could not create the version', msgOf(err));
                await explainRefusal(r, err, { payload, version });
              }
            }},
          ],
          onOpen(modal){
            const area = modal.querySelector('#nvBody');
            area.value = '';
            area.placeholder = 'Loading the current body…';
            if(!r.current_version){ area.value = '{}'; area.placeholder = ''; return; }
            API.configurations.version(r.id, r.current_version)
              .then(v => { area.value = JSON.stringify(v.payload || {}, null, 2); area.placeholder = ''; })
              .catch(err => { area.value = '{}'; area.placeholder = ''; toast('warn','Could not preload the body', msgOf(err)); });
          },
        });
      }

      function validateCfg(r){
        if(!allowed('member','Validating a configuration')) return;
        openModal({
          title:'Validation — ' + r.name, icon:'shieldCheck', wide:true,
          body: LOADING(160), footer:[{label:'Close'}],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.configurations.validate(r.id)
              .then(report => { body.innerHTML = validationHtml(report); })
              .catch(err => fail(body, err, null, 'the validation report'));
          },
        });
      }

      function showValidation(r, report){
        openModal({
          title:'Validation — ' + r.name, icon:'shieldCheck', wide:true,
          body: validationHtml(report), footer:[{label:'Close'}],
        });
      }

      function validationHtml(report){
        const findings = report.findings || [];
        return `<div class="flex" style="gap:14px;flex-wrap:wrap;margin-bottom:12px">
            ${kv([['Result', report.valid ? '<span class="st-green">Valid</span>' : '<span class="st-red">Invalid</span>']])}
            ${kv([['Version', mono(report.version)]])}
            ${kv([['Type', text(report.config_type)]])}
            ${kv([['Environment', text(report.environment)]])}
            ${kv([['Errors', String(report.error_count == null ? 0 : report.error_count)]])}
            ${kv([['Warnings', String(report.warning_count == null ? 0 : report.warning_count)]])}
            ${kv([['Fields checked', `${report.checked_fields == null ? '—' : report.checked_fields} of ${report.declared_fields == null ? '—' : report.declared_fields}`]])}
          </div>
          ${findings.length
            ? findingsTable(findings)
            : EMPTY('checkCircle','No findings','Every declared field checked out.')}`;
      }

      function findingsTable(findings){
        return `<table class="tbl"><thead><tr><th>Field</th><th>Severity</th><th>Code</th><th>Message</th></tr></thead><tbody>
            ${(findings || []).map(f=>`<tr style="cursor:default"><td class="mono">${esc(f.field || '—')}</td>
              <td>${badge(f.severity || 'info', f.severity === 'error' ? 'red' : f.severity === 'warning' ? 'amber' : 'gray')}</td>
              <td class="dim">${esc(f.code || '—')}</td><td>${esc(f.message || '')}</td></tr>`).join('')}
          </tbody></table>`;
      }

      /**
       * The per-field findings inside a 422. Activation, rollback, restore and a
       * re-typed edit are all refused with them attached, and a toast that only
       * says "failed validation" leaves the reader guessing which field.
       */
      function findingsOf(err){
        const d = err && err.status === 422 && err.details;
        return d && Array.isArray(d.findings) ? d : null;
      }

      /**
       * Open the validation report behind a refusal. The 422 names the fields
       * at fault but not the type, environment or field counts the report
       * shows, so /validate is asked for the full report of the same body
       * (`check` is {payload, version}, {version}, or nothing for the live
       * body). If that call fails, the findings the refusal carried are shown
       * on their own. Resolves false when the error was not a validation refusal.
       */
      async function explainRefusal(r, err, check){
        const refused = findingsOf(err);
        if(!refused) return false;
        let report = null;
        try { report = await API.configurations.validateBody(r.id, check || {}); }
        catch (e) { report = null; }
        if(!report || report.valid !== false){
          report = { valid:false, version:(check && check.version) || null,
            config_type:r.config_type, environment:r.environment, findings:refused.findings,
            error_count:refused.error_count, warning_count:refused.warning_count };
        }
        showValidation(r, report);
        return true;
      }

      function deprecateCfg(r){
        if(!allowed('admin','Deprecating a configuration')) return;
        openModal({
          title:'Deprecate Configuration', icon:'clock',
          body:`<p style="margin-top:0">Deprecate <b style="color:var(--text)">${esc(r.name)}</b> ${esc(r.current_version || '')}?</p>
            <p class="small">Agents migrate to the latest active version at their next deploy. This is recorded in the audit trail.</p>
            <div class="form-row"><label>REASON</label><input class="input" id="dpReason" placeholder="Why is it being retired?"></div>
            <label class="flex small" style="gap:7px;align-items:center"><input type="checkbox" id="dpArchive"> Archive instead of deprecate</label>`,
          footer:[
            {label:'Cancel'},
            {label:'Deprecate', cls:'danger', onClick: async (close, modal) => {
              const body = {
                reason: (modal.querySelector('#dpReason').value || '').trim() || null,
                archive: modal.querySelector('#dpArchive').checked,
              };
              try {
                const res = await Store.mutate(() => API.configurations.deprecate(r.id, body), { event:'configurations:changed' });
                close();
                toast('warn','Configuration retired', res.message || `${r.name} is now ${res.configuration ? res.configuration.status : 'deprecated'}.`);
                refreshAll();
                if(res.configuration) showCfg(res.configuration);
              } catch (err) {
                toast('error','Could not deprecate', msgOf(err));
              }
            }},
          ],
        });
      }

      async function restoreCfg(r){
        if(!allowed('admin','Restoring a configuration')) return;
        try {
          const res = await Store.mutate(() => API.configurations.restore(r.id), { event:'configurations:changed' });
          toast('success','Configuration restored', res.message || `${r.name} is active again.`);
          refreshAll();
          if(res.configuration) showCfg(res.configuration);
        } catch (err) {
          toast('error','Could not restore', msgOf(err));
          await explainRefusal(r, err);
        }
      }

      function activateVersion(r, version){
        if(!allowed('operator','Activating a version')) return;
        confirmModal({
          title:'Activate Version', icon:'checkCircle', confirmLabel:'Activate',
          body:`<p style="margin-top:0">Publish draft <b class="mono" style="color:var(--text)">${esc(version)}</b> as the live body of <b style="color:var(--text)">${esc(r.name)}</b>?</p>
            <p class="small">The server validates the draft first and refuses it, with the fields at fault, if it does not pass.</p>`,
          onConfirm: async () => {
            try {
              const res = await Store.mutate(() => API.configurations.activateVersion(r.id, version), { event:'configurations:changed' });
              toast('success','Version activated', res.message || `${r.name} ${version} is live.`);
              refreshAll();
              if(res.configuration){ showCfg(res.configuration); openTab(1); }
            } catch (err) {
              toast('error','Could not activate the version', msgOf(err));
              await explainRefusal(r, err, { version });
            }
          },
        });
      }

      function rollbackCfg(r, version){
        if(!allowed('admin','Rolling a configuration back')) return;
        openModal({
          title:'Roll Back — ' + r.name, icon:'replay',
          body:`<p style="margin-top:0">Republish <b class="mono" style="color:var(--text)">${esc(version)}</b> as the live body of <b style="color:var(--text)">${esc(r.name)}</b>?</p>
            <p class="small">A new revision is cut carrying the old body, so the history stays complete.</p>
            <div class="form-row"><label>CHANGE NOTE</label><input class="input" id="rbNote" value="Rolled back to ${esc(version)}"></div>`,
          footer:[
            {label:'Cancel'},
            {label:'Roll Back', cls:'danger', onClick: async (close, modal) => {
              try {
                const res = await Store.mutate(() => API.configurations.rollback(r.id, {
                  version, change_note: (modal.querySelector('#rbNote').value || '').trim() || null,
                }), { event:'configurations:changed' });
                close();
                toast('success','Rollback complete', res.message || `${r.name} restored to ${version}.`);
                refreshAll();
                if(res.configuration) showCfg(res.configuration);
              } catch (err) {
                toast('error','Could not roll back', msgOf(err));
                await explainRefusal(r, err, { version });
              }
            }},
          ],
        });
      }

      /* ------------------------------- import ------------------------------- */

      document.getElementById('cfImport').addEventListener('click', ()=>{
        if(!allowed('admin','Importing configurations')) return;
        openModal({
          title:'Import Configuration Bundle', icon:'upload', wide:true,
          body:`<div class="form-row"><label>BUNDLE (JSON)</label><textarea class="input" id="imBody" rows="12" placeholder='{"items":[…]}'></textarea></div>
            <div class="grid g2">
              <div class="form-row"><label>ON CONFLICT</label><select class="filter-select w-100" id="imMode" style="height:34px"><option value="skip">Skip existing</option><option value="new_version">Cut a new version</option></select></div>
              <div class="form-row"><label>&nbsp;</label><button class="btn w-100" id="imLoad">${ICONS.download}Load this workspace's bundle</button></div>
            </div>
            <div class="quote small">${ICONS.info} The server validates every item and reports what it created, versioned and skipped.</div>`,
          footer:[
            {label:'Cancel'},
            {label:'Import', cls:'primary', onClick: async (close, modal) => {
              let parsed;
              try { parsed = JSON.parse((modal.querySelector('#imBody').value || '').trim() || 'null'); }
              catch (e) { toast('error','Invalid bundle','The pasted text is not valid JSON: ' + e.message); return; }
              const items = parsed && Array.isArray(parsed.items) ? parsed.items : (Array.isArray(parsed) ? parsed : null);
              if(!items || !items.length){ toast('error','Nothing to import','The bundle has no items.'); return; }
              try {
                const res = await Store.mutate(() => API.configurations.importBundle({
                  items, on_conflict: modal.querySelector('#imMode').value, source:'Configuration Center',
                }), { event:'configurations:changed' });
                close();
                toast('success','Bundle imported',
                  `${res.created} created · ${res.versioned} versioned · ${res.skipped} skipped of ${res.submitted} submitted.`);
                if((res.issues || []).length){
                  openModal({ title:'Import issues', icon:'alert', wide:true,
                    body:`<table class="tbl"><thead><tr><th>#</th><th>Name</th><th>Reason</th></tr></thead><tbody>
                      ${res.issues.map(i=>`<tr style="cursor:default"><td class="num">${i.index}</td><td class="cell-main">${esc(i.name || '—')}</td><td>${esc(i.reason || '')}</td></tr>`).join('')}
                    </tbody></table>`, footer:[{label:'Close'}] });
                }
                refreshAll();
              } catch (err) {
                toast('error','Import failed', msgOf(err));
              }
            }},
          ],
          onOpen(modal){
            modal.querySelector('#imLoad').addEventListener('click', (e)=>{
              e.preventDefault();
              const area = modal.querySelector('#imBody');
              area.value = '';
              area.placeholder = 'Loading the current bundle…';
              API.configurations.bundle(table.params())
                .then(b => { area.value = JSON.stringify(b, null, 2); area.placeholder = ''; })
                .catch(err => { area.placeholder = ''; toast('error','Could not load the bundle', msgOf(err)); });
            });
          },
        });
      });

      /* ------------------------------ inspector ------------------------------ */

      let activeTab = 0;
      function openTab(i){
        const insp = document.getElementById('cfInspector');
        if(!insp) return;
        insp.querySelectorAll('[data-it]').forEach(x=>x.classList.toggle('active', Number(x.dataset.it) === i));
        activeTab = i;
        paintTab(i);
      }

      let current = null;
      function showCfg(r){
        if(!r) return;
        current = r;
        selectedId = r.id;
        const layout = document.getElementById('cfLayout');
        const insp = document.getElementById('cfInspector');
        if(!layout || !insp) return;
        layout.classList.remove('collapsed');
        insp.innerHTML = `
          <div class="insp-head">
            <span class="entity-ico" style="width:38px;height:38px;background:var(--purple-dim);color:var(--purple-bright)">${ICONS[TYPE_ICONS[r.config_type] || 'settings']}</span>
            <div class="grow"><div class="insp-title">${esc(r.name)}</div>
              <div class="flex" style="gap:6px;margin-top:4px">${r.config_type?badge(r.config_type, TYPE_COLORS[r.config_type]):''}${r.status?statusText(r.status, cfgStatusColor(r.status)):''}</div></div>
            <button class="icon-btn insp-close" id="cfClose">${ICONS.x}</button></div>
          <div class="tabs" style="margin:8px 0 0">${['Overview','Versions','Usage','Audit Trail'].map((t,i)=>
            `<div class="tab ${i===0?'active':''}" data-it="${i}" style="padding:8px 9px">${t}</div>`).join('')}</div>
          <div id="cfInspBody"></div>`;
        insp.querySelector('#cfClose').addEventListener('click', ()=>document.getElementById('cfLayout').classList.add('collapsed'));
        insp.querySelectorAll('[data-it]').forEach(tb=>tb.addEventListener('click', ()=>openTab(Number(tb.dataset.it))));
        activeTab = 0;
        paintTab(0);
      }

      function paintTab(i){
        const host = document.getElementById('cfInspBody');
        const r = current;
        if(!host || !r) return;
        if(i === 0) overviewTab(host, r);
        else if(i === 1) versionsTab(host, r);
        else if(i === 2) usageTab(host, r);
        else auditTab(host, r);
      }

      function overviewTab(host, r){
        host.innerHTML = `
          ${inspSection('Summary','info', kv([
            ['Name', esc(r.name)],
            ['Type', text(r.config_type)],
            ['Description', `<span class="small dim right">${r.description ? esc(r.description) : '—'}</span>`],
            ['Environment', r.environment?badge(r.environment):dash],
            ['Owner', r.owner_name ? esc(r.owner_name) + (r.owner_team ? ` (${esc(r.owner_team)})` : '') : dash],
            ['Created On', day(r.created_at)],
            ['Created By', text(r.created_by)],
            ['Last Modified', when(r.updated_at)],
            ['Last Modified By', text(r.updated_by)],
            ['Version', mono(r.current_version)],
            ['Revisions', num(r.version_count)],
            ['Status', r.status?statusText(r.status, cfgStatusColor(r.status)):dash],
            ['Impact', r.impact?riskBadge(r.impact):dash],
          ]))}
          <div class="insp-section"><div class="insp-section-title">Quick Actions</div>
            <div class="grid g2" style="gap:8px">
              <button class="btn sm" id="qaEdit">${ICONS.edit}Edit</button>
              <button class="btn sm" id="qaClone">${ICONS.copy}Clone</button>
              <button class="btn sm" id="qaVer">${ICONS.git}New Version</button>
              <button class="btn sm" id="qaVal">${ICONS.shieldCheck}Validate</button>
              ${(r.status === 'Deprecated' || r.status === 'Archived')
                ? `<button class="btn sm" id="qaRestore">${ICONS.refresh}Restore</button>`
                : `<button class="btn sm ghost-danger" id="qaDep">${ICONS.clock}Deprecate</button>`}
            </div></div>`;
        const bind = (id, fn, role, what) => {
          const el = host.querySelector(id);
          if(!el) return;
          if(role) requireRole(el, role, what);
          el.addEventListener('click', fn);
        };
        bind('#qaEdit', ()=>editCfg(r), 'operator', 'Editing a configuration');
        bind('#qaClone', ()=>cloneCfg(r), 'operator', 'Cloning a configuration');
        bind('#qaVer', ()=>newVersion(r), 'operator', 'Cutting a new version');
        bind('#qaVal', ()=>validateCfg(r), 'member', 'Validating a configuration');
        bind('#qaDep', ()=>deprecateCfg(r), 'admin', 'Deprecating a configuration');
        bind('#qaRestore', ()=>restoreCfg(r), 'admin', 'Restoring a configuration');
      }

      function versionsTab(host, r){
        host.innerHTML = inspSection('Version History','history', LOADING(150));
        API.configurations.versions(r.id, { page_size: 25 })
          .then(page => {
            const items = page.items || [];
            if(!items.length){
              host.innerHTML = inspSection('Version History','history',
                EMPTY('history','No revisions recorded','This configuration has no version history yet.'));
              return;
            }
            host.innerHTML = inspSection('Version History','history', `
              <div class="pipe">${items.map(v=>`
                <div class="pipe-step"><div class="pipe-dot ${v.is_current?'active':'done'}">${v.is_current?ICONS.star:ICONS.check}</div>
                  <div class="pipe-body">
                    <div class="pipe-title"><span class="mono">${esc(v.version)}${v.is_current?' — Current':''}</span><span class="faint">${rel(v.published_at || v.created_at)}</span></div>
                    <div class="pipe-sub">${esc(v.status || '')}${v.author_name?' · by '+esc(v.author_name):''}${v.change_note?' · '+esc(clip(v.change_note, 70)):''}</div>
                    <div class="flex" style="gap:6px;margin-top:5px">
                      <button class="btn sm" data-view="${esc(v.version)}">${ICONS.eye}View body</button>
                      ${v.is_current ? ''
                        : v.status === 'Draft'
                          ? `<button class="btn sm" data-activate="${esc(v.version)}">${ICONS.checkCircle}Activate</button>`
                          : `<button class="btn sm ghost-danger" data-roll="${esc(v.version)}">${ICONS.replay}Roll back</button>`}
                    </div>
                  </div></div>`).join('')}</div>
              <div class="grid g2" style="gap:8px;margin-top:10px">
                <div class="form-row" style="margin:0"><label>COMPARE FROM</label>
                  <select class="filter-select w-100" id="cfDiffFrom" style="height:30px">${items.map((v,i)=>`<option ${i===Math.min(1,items.length-1)?'selected':''}>${esc(v.version)}</option>`).join('')}</select></div>
                <div class="form-row" style="margin:0"><label>COMPARE TO</label>
                  <select class="filter-select w-100" id="cfDiffTo" style="height:30px">${items.map((v,i)=>`<option ${i===0?'selected':''}>${esc(v.version)}</option>`).join('')}</select></div>
              </div>
              <button class="btn sm" id="cfDiffGo" style="margin-top:8px">${ICONS.git}Compare versions</button>`);
            host.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click', ()=>viewVersion(r, b.dataset.view)));
            host.querySelectorAll('[data-roll]').forEach(b=>{
              requireRole(b, 'admin', 'Rolling a configuration back');
              b.addEventListener('click', ()=>rollbackCfg(r, b.dataset.roll));
            });
            // A Draft was never live, so it is published, not rolled back to —
            // the server refuses a rollback onto a body that was never current.
            host.querySelectorAll('[data-activate]').forEach(b=>{
              requireRole(b, 'operator', 'Activating a version');
              b.addEventListener('click', ()=>activateVersion(r, b.dataset.activate));
            });
            const go = host.querySelector('#cfDiffGo');
            if(go) go.addEventListener('click', ()=>diffVersions(r,
              host.querySelector('#cfDiffFrom').value, host.querySelector('#cfDiffTo').value));
          })
          .catch(err => fail(host, err, ()=>versionsTab(host, r), 'the version history'));
      }

      function viewVersion(r, version){
        openModal({
          title:`${r.name} — ${version}`, icon:'fileText', wide:true,
          body: LOADING(200), footer:[{label:'Close'}],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.configurations.version(r.id, version)
              .then(v => {
                body.innerHTML = `<div class="flex" style="gap:14px;flex-wrap:wrap;margin-bottom:10px">
                    ${kv([['Version', mono(v.version)]])}${kv([['Status', v.status?badge(v.status, cfgStatusColor(v.status)):dash]])}
                    ${kv([['Author', text(v.author_name)]])}${kv([['Published', when(v.published_at || v.created_at)]])}
                    ${kv([['Current', v.is_current?'<span class="st-green">Yes</span>':'No']])}
                  </div>
                  ${v.change_note?`<div class="quote small">${esc(v.change_note)}</div>`:''}
                  <pre class="quote" style="white-space:pre-wrap;max-height:340px;overflow:auto">${esc(JSON.stringify(v.payload || {}, null, 2))}</pre>`;
              })
              .catch(err => fail(body, err, null, 'this revision'));
          },
        });
      }

      function diffVersions(r, from, to){
        if(from === to){ toast('info','Same version','Pick two different revisions to compare.'); return; }
        openModal({
          title:`Diff — ${r.name}`, icon:'git', wide:true,
          body: LOADING(200), footer:[{label:'Close'}],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.configurations.versionDiff(r.id, { from, to })
              .then(d => {
                body.innerHTML = `<div class="flex" style="gap:14px;flex-wrap:wrap;margin-bottom:12px">
                    ${kv([['From', mono(d.from_version)]])}${kv([['To', mono(d.to_version)]])}
                    ${kv([['Added', String(d.added)]])}${kv([['Removed', String(d.removed)]])}${kv([['Changed', String(d.changed)]])}
                  </div>
                  ${d.identical
                    ? EMPTY('checkCircle','The two revisions are identical','Nothing changed between them.')
                    : `<table class="tbl"><thead><tr><th>Field</th><th>Change</th><th>Before</th><th>After</th></tr></thead><tbody>
                        ${(d.changes || []).map(c=>`<tr style="cursor:default"><td class="mono">${esc(c.field)}</td>
                          <td>${badge(c.change, c.change === 'added' ? 'green' : c.change === 'removed' ? 'red' : 'amber')}</td>
                          <td class="dim">${esc(JSON.stringify(c.before))}</td><td class="dim">${esc(JSON.stringify(c.after))}</td></tr>`).join('')}
                      </tbody></table>`}`;
              })
              .catch(err => fail(body, err, null, 'the diff'));
          },
        });
      }

      /**
       * Why the run figures are dashes. `telemetry_available: false` is not an
       * outage — a store that does not answer fails the whole call and lands in
       * the error state below. It means no bound agent's project answered, and
       * for seven of the nine types that is every time: the registry records
       * only which model an agent serves and which environment it runs in. The
       * note used to blame the telemetry engine, and sent people to chase an
       * outage that was not there.
       */
      function noRunsReason(r, u){
        if(u.used_by_agents > 0){
          return 'None of the agents bound to this configuration has a telemetry project that answered, so there are no run figures for this window.';
        }
        const tracked = r.config_type === 'Model' || r.config_type === 'Environment';
        return 'No registered agent is bound to this configuration, so there are no runs to attribute.' +
          (tracked ? '' : ' Agent binding is only tracked for Model and Environment configurations.');
      }

      function usageTab(host, r){
        host.innerHTML = inspSection('Impact & Usage','chart', LOADING(150));
        API.configurations.usage(r.id)
          .then(u => {
            host.innerHTML = `
              ${inspSection('Impact & Usage','chart', kv([
                ['Impact Level', u.impact?riskBadge(u.impact):dash],
                ['Used By Agents', num(u.used_by_agents)],
                [`Runs (${u.window_days || 30}d)`, num(u.runs_30d)],
                ['Avg Success Rate', pct(u.success_rate)],
                [`Errors (${u.window_days || 30}d)`, num(u.error_count_30d)],
              ]) + (u.telemetry_available === false
                ? `<div class="scan-note" style="margin-top:8px">${ICONS.info} ${esc(noRunsReason(r, u))}</div>`
                : ''))}
              ${inspSection('Linked Configurations','link', (u.links || []).length
                ? kv(u.links.map(l=>[l.slot, l.configuration_id
                    ? `<span class="link" data-cfg="${esc(l.configuration_id)}">${esc(l.name || l.configuration_id)}</span>${l.resolved?'':' <span class="st-amber small">(unresolved)</span>'}`
                    : `${esc(l.name || '—')} <span class="st-amber small">(unresolved)</span>`]))
                : EMPTY('link','No linked configurations','This configuration does not reference another.'))}`;
            host.querySelectorAll('[data-cfg]').forEach(el=>el.addEventListener('click', ()=>{
              API.configurations.get(el.dataset.cfg).then(showCfg).catch(err=>toast('error','Could not open', msgOf(err)));
            }));
          })
          .catch(err => fail(host, err, ()=>usageTab(host, r), 'the usage breakdown'));
      }

      function auditTab(host, r){
        host.innerHTML = inspSection('Audit Trail','history', LOADING(150));
        API.audit.list({ entity_id: r.id, page_size: 20, sort: '-occurred_at' })
          .then(page => {
            const items = page.items || [];
            host.innerHTML = inspSection('Audit Trail','history', items.length
              ? `<div class="pipe">${items.map(e=>`
                  <div class="pipe-step"><div class="pipe-dot done">${ICONS.check}</div>
                    <div class="pipe-body"><div class="pipe-title"><span>${esc(e.action)}</span><span class="faint">${rel(e.occurred_at)}</span></div>
                    <div class="pipe-sub">by ${esc(e.actor || 'system')}${e.detail?' · '+esc(clip(e.detail, 80)):''}</div>
                    ${e.prev_value || e.new_value ? `<div class="pipe-sub"><span class="faint">${esc(clip(e.prev_value || '—', 40))} → ${esc(clip(e.new_value || '—', 40))}</span></div>` : ''}
                    </div></div>`).join('')}</div>`
              : EMPTY('history','No audit events','Nothing has been recorded against this configuration yet.'));
          })
          .catch(err => fail(host, err, ()=>auditTab(host, r), 'the audit trail'));
      }
    },
  };

  /* ============================ PROMPT MANAGER ============================ */

  const PROMPT_STATUSES = ['Draft','In Review','Approved','Blocked'];
  const PROMPT_KPIS = ['Total Prompts','Approved','In Review','Blocked','Draft','Avg Success Rate'];

  function promptStatusColor(s){
    return s === 'Approved' ? 'green' : s === 'In Review' ? 'amber' : s === 'Blocked' ? 'red' : 'blue';
  }

  SCREENS['prompts'] = {
    title:'Prompt Studio',
    render(main){
      let current = null;

      main.innerHTML = `
        ${pageHead({title:'Prompt Studio', sub:'Author, version and review system prompts — and run them against a model to see what they actually produce.',
          actions:`${searchBox('pmSearch','Search prompts…')}
          <button class="btn" id="pmExport">${ICONS.download}Export</button>
          <button class="btn primary" id="pmNew">${ICONS.plus}New Prompt</button>`})}
        <div id="pmKpis">${kpiSkeleton(PROMPT_KPIS)}</div>
        <div class="with-inspector" id="pmLayout">
          <div id="pmTableWrap"></div>
          <div class="inspector" id="pmInspector">
            <div class="insp-head"><div><div class="insp-title">Prompt</div>
              <div class="insp-sub">Select a row to inspect it.</div></div></div>
          </div>
        </div>`;

      requireRole(document.getElementById('pmNew'), 'member', 'Authoring a prompt');

      function loadSummary(){
        const host = document.getElementById('pmKpis');
        if(!host) return;
        host.innerHTML = kpiSkeleton(PROMPT_KPIS);
        API.prompts.summary()
          .then(s => {
            if(!document.getElementById('pmKpis')) return;
            host.innerHTML = kpiRow([
              {label:'Total Prompts', value:num(s.total), icon:'pen', color:'purple',
                sub: s.prompts_with_telemetry == null ? null : `${fmtFull(s.prompts_with_telemetry)} with measured runs`},
              {label:'Approved', value:`<span class="st-green">${num(s.approved)}</span>`, sub:'In production use', icon:'checkCircle', color:'green'},
              {label:'In Review', value:`<span class="st-amber">${num(s.in_review)}</span>`, sub:'Awaiting sign-off', icon:'clock', color:'amber'},
              {label:'Blocked', value:`<span class="st-red">${num(s.blocked)}</span>`, sub:'Failed governance checks', icon:'xCircle', color:'red'},
              {label:'Draft', value:`<span class="st-blue">${num(s.draft)}</span>`, sub:'Not yet submitted', icon:'edit', color:'blue'},
              {label:'Avg Success Rate', value:pct(s.avg_success_rate), icon:'target', color:'blue',
                sub: s.runs_30d == null ? null : `over ${fmtFull(s.runs_30d)} runs in ${s.window_days || 30} days`},
            ], 190);
          })
          .catch(err => fail(host, err, loadSummary, 'the prompt summary'));
      }
      loadSummary();

      const table = dataTable({
        rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'prompts',
        searchPlaceholder:'Search by name, agent, tag…',
        defaultSort:{ key:'modified_at', dir:-1 },
        emptyText:'No prompts in this workspace yet',
        columns:[
          {key:'name', label:'Prompt', render:r=>entityCell(r.name, r.agent ? 'Agent: ' + r.agent : (r.description || ''), 'pen', 'purple')},
          {key:'version', label:'Version', render:r=>mono(r.version)},
          {key:'status', label:'Status', render:r=>r.status?badge(r.status, promptStatusColor(r.status)):dash},
          {key:'environment', label:'Environment', render:r=>r.environment?badge(r.environment):dash},
          {key:'estimated_tokens', label:'Tokens', align:'right', cls:'num', render:r=>num(r.estimated_tokens)},
          {key:'runs_30d', label:'Runs (30d)', align:'right', cls:'num', render:r=>num(r.runs_30d)},
          {key:'success_rate', label:'Success', align:'right', cls:'num', render:r=>pct(r.success_rate)},
          {key:'owner', label:'Owner', render:r=>r.owner?ownerCell(r.owner, ''):dash},
          {key:'modified_at', label:'Modified', render:r=>`<span class="dim nowrap">${rel(r.modified_at)}</span>`},
        ],
        filters:[
          {key:'status', label:'Status', param:'status', options:PROMPT_STATUSES, allLabel:'All Status'},
          {key:'env', label:'Environment', param:'env', options:ENVIRONMENTS, allLabel:'All Environments'},
          {key:'agent', label:'Agent', param:'agent', options:[], allLabel:'All Agents'},
        ],
        source: (params) => API.prompts.list(params),
        exportSource: (params) => API.prompts.export(params),
        autoSelectFirst: true,
        onSelect: showPrompt,
        rowActions: r=>[
          roleItem('member','Running a prompt', {label:'Run Prompt', icon:'play', onClick:()=>runPrompt(r)}),
          {label:'New Version', icon:'git', onClick:()=>newVersion(r)},
          roleItem('member','Testing a prompt', {label:'Test Prompt (render a dataset)', icon:'beaker', onClick:()=>testPrompt(r)}),
          {label:'View Diff', icon:'git', onClick:()=>{ showPrompt(r); }},
          {sep:true},
          ...lifecycleItems(r),
        ],
      });

      const wrap = document.getElementById('pmTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);
      document.getElementById('pmSearch').addEventListener('input', e=>table.search(e.target.value));
      document.getElementById('pmExport').addEventListener('click', ()=>table.export());

      // The agent dropdown carries the workspace's registered agents.
      API.prompts.agents({ page_size: 100, sort:'name' })
        .then(page => fillOptions(table, 2, (page.items || []).map(a=>a.name)))
        .catch(()=>{ /* the filter stays at "All Agents" */ });

      function refreshAll(){ table.refresh(); loadSummary(); }

      /* -------- the draft → in_review → approved / blocked lifecycle -------- */

      /* The server's transition table, so the console offers exactly the moves
         it will accept: Draft → In Review; In Review → Approved or Blocked;
         Approved → Blocked or back to In Review; Blocked → In Review. Block used
         to be offered on a Draft, which took a note and then answered 409, and
         an Approved prompt could not be sent back to review from here at all. */
      const canSubmit = (status) => status === 'Draft' || status === 'Blocked' || status === 'Approved';
      const canBlock = (status) => status === 'In Review' || status === 'Approved';
      const submitLabel = (status) => status === 'Approved' ? 'Re-submit for Review' : 'Submit for Review';

      function lifecycleItems(r){
        const items = [];
        if(canSubmit(r.status)){
          items.push(roleItem('member','Submitting a prompt for review',
            {label:submitLabel(r.status), icon:'send', onClick:()=>lifecycle(r, 'submitReview', submitLabel(r.status))}));
        }
        if(r.status === 'In Review'){
          items.push(roleItem('operator','Approving a prompt',
            {label:'Approve', icon:'checkCircle', onClick:()=>lifecycle(r, 'approve', 'Approve')}));
        }
        if(canBlock(r.status)){
          items.push(roleItem('operator','Blocking a prompt',
            {label:'Block', icon:'xCircle', danger:true, onClick:()=>lifecycle(r, 'block', 'Block')}));
        }
        return items;
      }

      const LIFECYCLE_ROLE = { submitReview:'member', approve:'operator', block:'operator' };
      const LIFECYCLE_DONE = { submitReview:'Submitted for review', approve:'Approved', block:'Blocked' };

      function lifecycle(r, verb, label){
        const role = LIFECYCLE_ROLE[verb];
        if(!allowed(role, label + ' a prompt')) return;
        openModal({
          title:`${label} — ${r.name}`, icon: verb === 'block' ? 'xCircle' : verb === 'approve' ? 'checkCircle' : 'send',
          body:`<p style="margin-top:0">${esc(label)} <b style="color:var(--text)">${esc(r.name)}</b> ${esc(r.version || '')}?</p>
            <div class="form-row"><label>NOTE</label><textarea class="input" id="lcNote" rows="2" placeholder="Recorded against the transition."></textarea></div>`,
          footer:[
            {label:'Cancel'},
            {label, cls: verb === 'block' ? 'danger' : 'primary', onClick: async (close, modal) => {
              const note = (modal.querySelector('#lcNote').value || '').trim() || null;
              try {
                const res = await Store.mutate(() => API.prompts[verb](r.id, { note }), { event:'prompts:changed' });
                close();
                const p = res.prompt || {};
                toast(verb === 'block' ? 'warn' : 'success', LIFECYCLE_DONE[verb],
                  res.message || `${p.name || r.name}: ${res.previous_status || r.status} → ${p.status || ''}.`);
                refreshAll();
                if(res.prompt) showSaved(res.prompt);
              } catch (err) {
                toast('error', `Could not ${label.toLowerCase()}`, msgOf(err));
              }
            }},
          ],
        });
      }

      /* ------------------------------ mutations ------------------------------ */

      document.getElementById('pmNew').addEventListener('click', async ()=>{
        if(!allowed('member','Authoring a prompt')) return;
        let agents = [];
        try { agents = (await API.prompts.agents({ page_size: 100, sort:'name' })).items || []; }
        catch (err) { toast('warn','Agent list unavailable', msgOf(err)); }
        openModal({
          title:'New Prompt', icon:'pen', wide:true,
          body:`<div class="form-row"><label>PROMPT NAME</label><input class="input" id="npName" placeholder="e.g. Vendor Email Tone Prompt"></div>
            <div class="grid g2">
              <div class="form-row"><label>AGENT</label><select class="filter-select w-100" id="npAgent" style="height:34px"><option value="">Unassigned</option>${agents.map(a=>`<option>${esc(a.name)}</option>`).join('')}</select></div>
              <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="npEnv" style="height:34px">${optionList(ENVIRONMENTS,'Development')}</select></div>
            </div>
            <div class="form-row"><label>DESCRIPTION</label><input class="input" id="npDesc" placeholder="What is this prompt for?"></div>
            <div class="form-row"><label>PROMPT TEXT</label><textarea class="input" id="npText" rows="7" placeholder="You are…"></textarea></div>
            <div class="grid g2">
              <div class="form-row"><label>VERSION</label><input class="input" id="npVer" value="v0.1.0"></div>
              <div class="form-row"><label>CHANGE NOTE</label><input class="input" id="npNote" value="Initial draft"></div>
            </div>`,
          footer:[
            {label:'Cancel'},
            {label:'Create Draft', cls:'primary', onClick: async (close, modal) => {
              const name = (modal.querySelector('#npName').value || '').trim();
              const template = modal.querySelector('#npText').value || '';
              if(!name){ toast('error','Name required','Give the prompt a name.'); return; }
              if(!template.trim()){ toast('error','Template required','A prompt needs a body.'); return; }
              try {
                const created = await Store.mutate(() => API.prompts.create({
                  name, template,
                  description: (modal.querySelector('#npDesc').value || '').trim() || null,
                  agent: modal.querySelector('#npAgent').value || null,
                  environment: modal.querySelector('#npEnv').value,
                  version: (modal.querySelector('#npVer').value || '').trim() || 'v0.1.0',
                  change_note: (modal.querySelector('#npNote').value || '').trim() || null,
                }), { event:'prompts:changed' });
                close();
                toast('success','Prompt created', `${created.name} ${created.version || ''} saved as ${created.status}.`);
                refreshAll();
              } catch (err) {
                toast('error','Could not create the prompt', msgOf(err));
              }
            }},
          ],
        });
      });

      function newVersion(r){
        if(!allowed('member','Committing a prompt version')) return;
        openModal({
          title:'New Version — ' + r.name, icon:'git', wide:true,
          body:`<div class="grid g2">
              <div class="form-row"><label>CURRENT VERSION</label><input class="input" value="${esc(r.version || '—')}" disabled></div>
              <div class="form-row"><label>NEW VERSION</label><input class="input" id="pvVer" placeholder="leave blank to auto-increment"></div>
            </div>
            <div class="form-row"><label>TEMPLATE</label><textarea class="input" id="pvText" rows="10"></textarea></div>
            <div class="form-row"><label>CHANGE NOTE</label><input class="input" id="pvNote" placeholder="What changed?"></div>`,
          footer:[
            {label:'Cancel'},
            {label:'Commit Version', cls:'primary', onClick: async (close, modal) => {
              const template = modal.querySelector('#pvText').value || '';
              if(!template.trim()){ toast('error','Template required','A commit needs a body.'); return; }
              try {
                const res = await Store.mutate(() => API.prompts.createVersion(r.id, {
                  template,
                  version: (modal.querySelector('#pvVer').value || '').trim() || null,
                  change_note: (modal.querySelector('#pvNote').value || '').trim() || null,
                }), { event:'prompts:changed' });
                close();
                toast('success','Version committed', res.message || `${r.name} ${res.version ? res.version.version : ''} is now the head.`);
                refreshAll();
                if(res.prompt) showSaved(res.prompt);
              } catch (err) {
                toast('error','Could not commit the version', msgOf(err));
              }
            }},
          ],
          onOpen(modal){
            const area = modal.querySelector('#pvText');
            area.value = r.template || '';
            if(r.template) return;
            area.placeholder = 'Loading the current template…';
            API.prompts.get(r.id)
              .then(p => { area.value = p.template || ''; area.placeholder = ''; })
              .catch(err => { area.placeholder = ''; toast('warn','Could not preload the template', msgOf(err)); });
          },
        });
      }

      /**
       * What a commit is called on screen. A label is optional, and it is not
       * unique either — a restore re-commits the old label, and the next
       * auto-increment then mints one that already exists — so the label is
       * only ever shown. Everything sent to the server is the commit.
       */
      function commitLabel(v){
        return v.version || (v.commit ? String(v.commit).slice(0, 8) : '—');
      }

      /**
       * Restore by COMMIT. The button used to send the label, and the server
       * resolves a label to the newest commit carrying it: with two "v0.2.0" in
       * the history, Restore on the older row re-committed the head, reported
       * success and reset the approval — the body asked for never came back.
       */
      function restoreVersion(r, commit, label){
        if(!allowed('operator','Restoring a prompt version')) return;
        const version = label || String(commit).slice(0, 8);
        confirmModal({
          title:'Restore Version', icon:'replay', confirmLabel:'Restore', danger:true,
          msg:`Re-commit ${version} (commit ${String(commit).slice(0, 8)}) as the head of ${r.name}? The history is kept — a new commit carries the old body.`,
          onConfirm: async () => {
            try {
              const res = await Store.mutate(() => API.prompts.restore(r.id, commit), { event:'prompts:changed' });
              toast('success','Version restored', res.message || `${r.name} restored to ${version}.`);
              refreshAll();
              if(res.prompt) showSaved(res.prompt);
            } catch (err) {
              toast('error','Could not restore', msgOf(err));
            }
          },
        });
      }

      /* Run one prompt against a model and show what came back.
       *
       * The counterpart to Test Prompt, which renders and scores a whole
       * dataset without calling anything. This is the single interactive
       * question a prompt author actually asks: what does this say, how long
       * did it take, and what did it cost.
       */
      function runPrompt(r){
        if(!allowed('member','Running a prompt')) return;
        openModal({
          title:'Run Prompt — ' + r.name, icon:'play', wide:true,
          body:`<div class="grid g2">
              <div class="form-row"><label>VARIABLES (JSON OBJECT)</label>
                <textarea class="input mono" id="rpVars" rows="5" placeholder='{"customer":"Acme"}'>{}</textarea></div>
              <div class="form-row"><label>SYSTEM MESSAGE (OPTIONAL)</label>
                <textarea class="input" id="rpSys" rows="5" placeholder="Placed before the prompt"></textarea></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>MODEL (OPTIONAL)</label>
                <input class="input" id="rpModel" placeholder="Leave blank to use the deployment default"></div>
              <div class="form-row"><label>MAX OUTPUT TOKENS</label>
                <input class="input" id="rpMax" placeholder="Leave blank for the deployment cap"></div>
            </div>
            <div id="rpOut" style="margin-top:12px"></div>`,
          footer:[
            {label:'Close'},
            {label:'Run', cls:'primary', close:false, onClick: async (close, modal) => {
              const out = modal.querySelector('#rpOut');
              let variables;
              try { variables = JSON.parse(modal.querySelector('#rpVars').value || '{}'); }
              catch (e) { toast('error','Invalid variables','Not valid JSON: ' + e.message); return; }
              if(!variables || typeof variables !== 'object' || Array.isArray(variables)){
                toast('error','Invalid variables','Provide a JSON object of variable names to values.'); return;
              }
              const body = { variables };
              const model = modal.querySelector('#rpModel').value.trim();
              const system = modal.querySelector('#rpSys').value.trim();
              const cap = parseInt(modal.querySelector('#rpMax').value, 10);
              if(model) body.model = model;
              if(system) body.system = system;
              if(!isNaN(cap) && cap > 0) body.max_output_tokens = cap;

              out.innerHTML = LOADING(140);
              try {
                const res = await API.prompts.execute(r.id, body);
                const total = res.total_tokens != null ? res.total_tokens
                  : (res.prompt_tokens || 0) + (res.completion_tokens || 0);
                out.innerHTML = `
                  <div class="flex" style="gap:14px;flex-wrap:wrap;margin-bottom:10px">
                    ${kv([['Model', esc(res.model || '—')]])}
                    ${kv([['Tokens', total ? fmtFull(total) : '—']])}
                    ${kv([['Latency', res.latency_ms != null ? (res.latency_ms/1000).toFixed(2)+'s' : '—']])}
                    ${kv([['Commit', mono(res.commit)]])}
                  </div>
                  ${res.truncated ? `<div class="quote small st-amber">${ICONS.alert} The model stopped on the token cap, so this answer is cut short. Raise Max Output Tokens to see the rest.</div>` : ''}
                  ${(res.missing_variables || []).length ? `<div class="quote small st-amber">${ICONS.alert} Rendered without ${esc(res.missing_variables.join(', '))} — the template declares them and no value was supplied.</div>` : ''}
                  <div class="form-row"><label>OUTPUT</label>
                    <pre class="quote" style="white-space:pre-wrap;max-height:320px;overflow:auto">${esc(res.output || '(the model returned nothing)')}</pre></div>
                  <details><summary class="small dim" style="cursor:pointer">Prompt exactly as sent</summary>
                    <pre class="quote" style="white-space:pre-wrap;max-height:220px;overflow:auto;margin-top:8px">${esc(res.rendered || '')}</pre>
                  </details>`;
              } catch (err) {
                out.innerHTML = '';
                // A timeout is the console giving up, not the run being
                // cancelled: the model may answer after we stop waiting, and
                // that answer is billed. "Could not load this prompt run" reads
                // as "nothing happened" and invites a second run on top of it.
                if(err && err.code === 'timeout'){
                  out.innerHTML = `<div class="quote small st-amber">${ICONS.alert} No answer came back before the console stopped waiting. The run may still have finished on the server — and been billed — so check this prompt's Audit Trail before running it again.</div>`;
                  return;
                }
                // A deployment with no model configured is the common case, and
                // the server names the missing setting; show that, not a generic
                // failure.
                out.appendChild(screenError(err, null, 'this prompt run'));
              }
            }},
          ],
        });
      }

      function testPrompt(r){
        if(!allowed('member','Testing a prompt')) return;
        openModal({
          title:'Test Prompt — ' + r.name, icon:'beaker', wide:true,
          body:`<div class="form-row"><label>SAMPLE VARIABLE SETS (JSON ARRAY)</label>
              <textarea class="input" id="tpCases" rows="6" placeholder='[{"customer":"Acme"},{"customer":"Globex"}]'>[{}]</textarea></div>
            <label class="flex small" style="gap:7px;align-items:center"><input type="checkbox" id="tpScore" checked> Record the run as a dataset and experiment for evaluation</label>
            <div id="tpResult" style="margin-top:12px"></div>`,
          footer:[
            {label:'Close'},
            {label:'Run Test', cls:'primary', close:false, onClick: async (close, modal) => {
              const out = modal.querySelector('#tpResult');
              let cases;
              try { cases = JSON.parse(modal.querySelector('#tpCases').value || '[]'); }
              catch (e) { toast('error','Invalid cases','The variable sets are not valid JSON: ' + e.message); return; }
              if(!Array.isArray(cases)) { toast('error','Invalid cases','Provide a JSON array of objects.'); return; }
              out.innerHTML = LOADING(120);
              try {
                const res = await API.prompts.test(r.id, { cases, score: modal.querySelector('#tpScore').checked });
                out.innerHTML = `<div class="flex" style="gap:14px;flex-wrap:wrap;margin-bottom:10px">
                    ${kv([['Passed', `<span class="st-green">${res.passed}</span>`]])}
                    ${kv([['Failed', res.failed ? `<span class="st-red">${res.failed}</span>` : '0']])}
                    ${kv([['Commit', mono(res.commit)]])}
                    ${kv([['Variables', (res.variables || []).length ? esc(res.variables.join(', ')) : '—']])}
                    ${kv([['Recorded', res.recorded ? '<span class="st-green">Yes</span>' : 'No']])}
                    ${res.dataset_name ? kv([['Dataset', esc(res.dataset_name)]]) : ''}
                    ${res.experiment_name ? kv([['Experiment', esc(res.experiment_name)]]) : ''}
                  </div>
                  ${res.detail ? `<div class="quote small">${esc(res.detail)}</div>` : ''}
                  <table class="tbl"><thead><tr><th>#</th><th>Result</th><th>Missing</th><th>Unresolved</th><th>Tokens</th><th>Rendered</th></tr></thead><tbody>
                    ${(res.cases || []).map(c=>`<tr style="cursor:default"><td class="num">${c.index}</td>
                      <td>${c.ok?statusText('Passed','green'):statusText('Failed','red')}</td>
                      <td class="dim">${(c.missing_variables || []).length ? esc(c.missing_variables.join(', ')) : '—'}</td>
                      <td class="dim">${(c.unresolved_placeholders || []).length ? esc(c.unresolved_placeholders.join(', ')) : '—'}</td>
                      <td class="num">${num(c.estimated_tokens)}</td>
                      <td class="dim">${esc(clip(c.rendered || '', 90))}</td></tr>`).join('')}
                  </tbody></table>`;
                toast(res.failed ? 'warn' : 'success', 'Prompt test complete',
                  `${res.passed} passed, ${res.failed} failed.`);
              } catch (err) {
                fail(out, err, null, 'the prompt test');
                toast('error','Prompt test failed', msgOf(err));
              }
            }},
          ],
        });
      }

      /* ------------------------------ inspector ------------------------------ */

      function showPrompt(row){
        if(!row) return;
        current = row;
        const layout = document.getElementById('pmLayout');
        const insp = document.getElementById('pmInspector');
        if(!layout || !insp) return;
        layout.classList.remove('collapsed');
        insp.innerHTML = `<div class="insp-head"><div class="grow">
            <div class="insp-title">${esc(row.name)}</div>
            <div class="insp-sub">Loading…</div></div>
            <button class="icon-btn insp-close" id="pmClose">${ICONS.x}</button></div>${LOADING(220)}`;
        insp.querySelector('#pmClose').addEventListener('click', ()=>layout.classList.add('collapsed'));
        API.prompts.get(row.id)
          .then(p => { if(current && current.id === row.id) paintPrompt(insp, p); })
          .catch(err => {
            if(!current || current.id !== row.id) return;
            const holder = insp.querySelector('.card-loading');
            if(holder) holder.replaceWith(screenError(err, ()=>showPrompt(row), 'this prompt'));
          });
      }

      /**
       * Paint the inspector from a mutation's own answer. Every verb here hands
       * back the prompt as it now stands, the same shape GET /prompts/{id}
       * serves, so going through showPrompt fetched it a second time — one more
       * registry read and the audit queries behind it, after every click.
       */
      function showSaved(p){
        const layout = document.getElementById('pmLayout');
        const insp = document.getElementById('pmInspector');
        if(!layout || !insp) return;
        layout.classList.remove('collapsed');
        paintPrompt(insp, p);
      }

      function paintPrompt(insp, p){
        current = p;
        insp.innerHTML = `
          <div class="insp-head"><div class="grow">
            <div class="insp-title">${esc(p.name)}</div>
            <div class="flex" style="gap:6px;margin-top:5px">${p.status?badge(p.status, promptStatusColor(p.status)):''}<span class="mono small">${esc(p.version || '')}</span></div></div>
            <button class="icon-btn insp-close" id="pmClose">${ICONS.x}</button></div>
          ${inspSection('Prompt Text','fileText', `<div class="quote" style="white-space:pre-wrap;max-height:210px;overflow-y:auto">${p.template ? esc(p.template) : '—'}</div>
            <div class="flex between small faint" style="margin-top:5px"><span>${p.estimated_tokens == null ? '—' : fmtFull(p.estimated_tokens) + ' tokens'}</span><span>${esc(p.environment || '')}</span></div>
            ${(p.variables || []).length ? `<div class="small" style="margin-top:6px">Variables: ${p.variables.map(v=>`<span class="mono">${esc(v)}</span>`).join(', ')}</div>` : ''}`)}
          ${inspSection('Details','info', kv([
            ['Description', `<span class="small dim right">${p.description ? esc(p.description) : '—'}</span>`],
            ['Agent', p.agent_id
              ? `<span class="link" data-nav="agent/${esc(p.agent_id)}">${esc(p.agent)}</span>`
              : text(p.agent)],
            ['Owner', text(p.owner)],
            ['Commit', mono(p.commit ? String(p.commit).slice(0,12) : null)],
            ['Revisions', num(p.version_count)],
            ['Created', when(p.created_at)],
            ['Modified', when(p.modified_at)],
            ['Status Changed', p.status_changed_at ? `${when(p.status_changed_at)}${p.status_changed_by ? ' by ' + esc(p.status_changed_by) : ''}` : dash],
            ['Runs (30d)', num(p.runs_30d)],
            ['Success Rate', pct(p.success_rate)],
            ['Tags', (p.tags || []).length ? p.tags.map(t=>badge(t,'gray')).join(' ') : dash],
          ]))}
          <div id="pmVersions">${inspSection('Version History','history', LOADING(120))}</div>
          <div class="insp-section"><div class="insp-section-title">Lifecycle</div>
            <div class="grid g2" style="gap:8px">
              <button class="btn sm" id="pmSubmit">${ICONS.send}${esc(submitLabel(p.status))}</button>
              <button class="btn sm success" id="pmApprove">${ICONS.checkCircle}Approve</button>
              <button class="btn sm ghost-danger" id="pmBlock">${ICONS.xCircle}Block</button>
              <button class="btn sm" id="pmTest">${ICONS.beaker}Test Prompt</button>
              <button class="btn sm" id="pmVersion">${ICONS.git}New Version</button>
            </div></div>`;
        insp.querySelector('#pmClose').addEventListener('click', ()=>document.getElementById('pmLayout').classList.add('collapsed'));

        const submit = insp.querySelector('#pmSubmit');
        requireRole(submit, 'member', 'Submitting a prompt for review');
        if(Store.session.can('member') && !canSubmit(p.status)){
          submit.disabled = true;
          submit.title = `${p.name} is already ${p.status}.`;
        }
        submit.addEventListener('click', ()=>lifecycle(p, 'submitReview', submitLabel(p.status)));

        const approve = insp.querySelector('#pmApprove');
        requireRole(approve, 'operator', 'Approving a prompt');
        if(Store.session.can('operator') && p.status !== 'In Review'){
          approve.disabled = true;
          approve.title = 'Only a prompt in review can be approved.';
        }
        approve.addEventListener('click', ()=>lifecycle(p, 'approve', 'Approve'));

        const block = insp.querySelector('#pmBlock');
        requireRole(block, 'operator', 'Blocking a prompt');
        if(Store.session.can('operator') && !canBlock(p.status)){
          block.disabled = true;
          block.title = p.status === 'Blocked' ? 'This prompt is already blocked.'
            : 'Only a prompt that is in review or approved can be blocked.';
        }
        block.addEventListener('click', ()=>lifecycle(p, 'block', 'Block'));

        requireRole(insp.querySelector('#pmTest'), 'member', 'Testing a prompt')
          .addEventListener('click', ()=>testPrompt(p));
        requireRole(insp.querySelector('#pmVersion'), 'member', 'Committing a prompt version');
        insp.querySelector('#pmVersion').addEventListener('click', ()=>newVersion(p));

        loadVersions(p);
      }

      function loadVersions(p){
        const host = document.getElementById('pmVersions');
        if(!host) return;
        host.innerHTML = inspSection('Version History','history', LOADING(120));
        API.prompts.versions(p.id, { page_size: 25 })
          .then(page => {
            const items = page.items || [];
            if(!document.getElementById('pmVersions')) return;
            if(!items.length){
              host.innerHTML = inspSection('Version History','history',
                EMPTY('history','No commits recorded','This prompt has no version history yet.'));
              return;
            }
            // Two commits can carry one label; the short commit tells them apart.
            const carried = {};
            items.forEach(v => { carried[commitLabel(v)] = (carried[commitLabel(v)] || 0) + 1; });
            const shown = (v) => carried[commitLabel(v)] > 1 && v.commit
              ? `${commitLabel(v)} · ${String(v.commit).slice(0, 8)}` : commitLabel(v);
            host.innerHTML = inspSection('Version History','history', `
              <div class="pipe">${items.map(v=>`
                <div class="pipe-step"><div class="pipe-dot ${v.is_head?'active':'done'}">${v.is_head?ICONS.star:ICONS.check}</div>
                  <div class="pipe-body">
                    <div class="pipe-title"><span class="mono">${esc(shown(v))}${v.is_head?' — Head':''}</span><span class="faint">${rel(v.created_at)}</span></div>
                    <div class="pipe-sub">${esc(v.status || '')}${v.author?' · by '+esc(v.author):''}${v.change_note?' · '+esc(clip(v.change_note, 70)):''}</div>
                    <div class="flex" style="gap:6px;margin-top:5px">
                      <button class="btn sm" data-pv="${esc(v.commit)}">${ICONS.eye}View</button>
                      ${v.is_head || !v.commit ? '' : `<button class="btn sm" data-pr="${esc(v.commit)}" data-pl="${esc(commitLabel(v))}">${ICONS.replay}Restore</button>`}
                    </div>
                  </div></div>`).join('')}</div>
              <div class="grid g2" style="gap:8px;margin-top:10px">
                <div class="form-row" style="margin:0"><label>DIFF FROM</label>
                  <select class="filter-select w-100" id="pmDiffFrom" style="height:30px">${items.map((v,i)=>`<option value="${esc(v.commit)}" ${i===Math.min(1,items.length-1)?'selected':''}>${esc(shown(v))}</option>`).join('')}</select></div>
                <div class="form-row" style="margin:0"><label>DIFF TO</label>
                  <select class="filter-select w-100" id="pmDiffTo" style="height:30px">${items.map((v,i)=>`<option value="${esc(v.commit)}" ${i===0?'selected':''}>${esc(shown(v))}</option>`).join('')}</select></div>
              </div>
              <button class="btn sm" id="pmDiffGo" style="margin-top:8px">${ICONS.git}Compare commits</button>`);
            host.querySelectorAll('[data-pv]').forEach(b=>b.addEventListener('click', ()=>viewCommit(p, b.dataset.pv)));
            host.querySelectorAll('[data-pr]').forEach(b=>{
              requireRole(b, 'operator', 'Restoring a prompt version');
              b.addEventListener('click', ()=>restoreVersion(p, b.dataset.pr, b.dataset.pl));
            });
            const go = host.querySelector('#pmDiffGo');
            if(go) go.addEventListener('click', ()=>diffCommits(p,
              host.querySelector('#pmDiffFrom').value, host.querySelector('#pmDiffTo').value));
          })
          .catch(err => fail(host, err, ()=>loadVersions(p), 'the version history'));
      }

      function viewCommit(p, commit){
        openModal({
          title:`${p.name} — commit ${String(commit).slice(0,10)}`, icon:'fileText', wide:true,
          body: LOADING(200), footer:[{label:'Close'}],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.prompts.version(p.id, commit)
              .then(v => {
                body.innerHTML = `<div class="flex" style="gap:14px;flex-wrap:wrap;margin-bottom:10px">
                    ${kv([['Version', mono(v.version)]])}${kv([['Status', v.status?badge(v.status, promptStatusColor(v.status)):dash]])}
                    ${kv([['Author', text(v.author)]])}${kv([['Committed', when(v.created_at)]])}
                    ${kv([['Tokens', num(v.estimated_tokens)]])}${kv([['Head', v.is_head?'<span class="st-green">Yes</span>':'No']])}
                  </div>
                  ${v.change_note?`<div class="quote small">${esc(v.change_note)}</div>`:''}
                  ${(v.variables || []).length ? `<div class="small" style="margin:6px 0">Variables: ${v.variables.map(x=>`<span class="mono">${esc(x)}</span>`).join(', ')}</div>` : ''}
                  <pre class="quote" style="white-space:pre-wrap;max-height:340px;overflow:auto">${esc(v.template || '')}</pre>`;
              })
              .catch(err => fail(body, err, null, 'this commit'));
          },
        });
      }

      function diffCommits(p, from, to){
        if(from === to){ toast('info','Same commit','Pick two different commits to compare.'); return; }
        openModal({
          title:'Diff — ' + p.name, icon:'git', wide:true,
          body: LOADING(200), footer:[{label:'Close'}],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.prompts.diff(p.id, { from, to })
              .then(d => {
                body.innerHTML = `<div class="flex" style="gap:14px;flex-wrap:wrap;margin-bottom:12px">
                    ${kv([['From', mono(d.from_version)]])}${kv([['To', mono(d.to_version)]])}
                    ${kv([['Added lines', `<span class="st-green">+${d.added_lines}</span>`]])}
                    ${kv([['Removed lines', `<span class="st-red">−${d.removed_lines}</span>`]])}
                  </div>
                  ${d.identical
                    ? EMPTY('checkCircle','The two commits are identical','Nothing changed between them.')
                    : `<pre class="quote" style="white-space:pre-wrap;max-height:380px;overflow:auto">${(d.lines || []).map(l=>{
                        const cls = l.kind === 'added' ? 'st-green' : l.kind === 'removed' ? 'st-red' : 'faint';
                        const mark = l.kind === 'added' ? '+' : l.kind === 'removed' ? '-' : ' ';
                        return `<span class="${cls}">${esc(mark + ' ' + (l.text || ''))}</span>`;
                      }).join('\n')}</pre>`}`;
              })
              .catch(err => fail(body, err, null, 'the diff'));
          },
        });
      }
    },
  };

  /* ==================== RAG & KNOWLEDGE GOVERNANCE ==================== */

  const KS_TYPES = ['SharePoint','Blob Storage','Confluence','Web','Database','Vector Index','GitHub','File Share'];
  const KS_STATUSES = ['Active','Syncing','Error','Failed','Paused'];
  const SENSITIVITIES = ['Public','Internal','Confidential','Highly Confidential','Restricted'];
  const KS_KPIS = ['Total Sources','Active Sources','Total Documents','Total Chunks','Avg. Grounding Score','Sources with ACL'];
  const KS_LOGO = { 'SharePoint':'sharepoint', 'Blob Storage':'azureblob', 'Confluence':'confluence',
    'Web':'webcrawl', 'Database':'sql', 'Vector Index':'vector', 'GitHub':'github', 'File Share':'fileshare' };
  const KS_TABS = ['Knowledge Sources','Vector Indexes','Retrieval Policies','Citations & Grounding','Data Access & Lineage'];

  function ksStatusColor(s){
    return s === 'Active' ? 'green' : s === 'Syncing' ? 'amber' : (s === 'Failed' || s === 'Error') ? 'red' : 'gray';
  }

  const ACL_ENFORCEMENTS = ['Enforced','Enforced + audit','Not required'];

  /**
   * A source carries two different counts, and only one of them was on screen.
   * `document_count` / `chunk_count` are the inventory an indexer REPORTED — 0
   * until somebody reports one. What a sync actually finds is what retrieval
   * telemetry saw in its window, kept in settings.observed_*: not the corpus
   * size, so it is never folded into the reported figure, but after a sync
   * that announced "42 documents, 310 chunks" it is the number the reader is
   * looking for. It rides under the reported count as a second line.
   */
  function seenLine(r, key){
    const v = r.settings && r.settings[key];
    return v == null ? '' : `<div class="cell-sub">${fmtFull(v)} seen in retrieval</div>`;
  }

  SCREENS['knowledge'] = {
    title:'RAG & Knowledge Governance',
    render(main){
      let current = null;
      const owners = ownerDirectory(() => API.knowledge.owners());
      const pollers = Object.create(null);

      main.innerHTML = `
        ${pageHead({title:'RAG & Knowledge Governance', sub:'Manage knowledge sources, vector indexes, retrieval policies, citations, and grounding quality.',
          actions:`${searchBox('ksSearch','Search knowledge sources…')}
          <button class="btn" id="ksExport">${ICONS.download}Export</button>
          <button class="btn primary" id="ksAdd">${ICONS.plus}Add Knowledge Source</button>`})}
        <div id="ksTabs"></div>
        <div id="ksKpis">${kpiSkeleton(KS_KPIS)}</div>
        <div class="with-inspector" id="ksLayout">
          <div id="ksTableWrap"></div>
          <div class="inspector" id="ksInspector">
            <div class="insp-head"><div><div class="insp-title">Knowledge Source</div>
              <div class="insp-sub">Select a row to inspect it.</div></div></div>
          </div>
        </div>`;

      requireRole(document.getElementById('ksAdd'), 'operator', 'Adding a knowledge source');

      function loadSummary(){
        const host = document.getElementById('ksKpis');
        if(!host) return;
        host.innerHTML = kpiSkeleton(KS_KPIS);
        API.knowledge.summary()
          .then(s => {
            if(!document.getElementById('ksKpis')) return;
            host.innerHTML = kpiRow([
              {label:'Total Sources', value:num(s.total_sources), icon:'book', color:'purple',
                delta: s.created_30d == null ? null : String(s.created_30d), dir: deltaDir(s.created_30d), good:true,
                vs:`added in the last ${s.window_days || 30} days`},
              {label:'Active Sources', value:`<span class="st-green">${num(s.active_sources)}</span>`,
                sub: s.active_percent == null ? null : pct(s.active_percent, 0) + ' of total', icon:'checkCircle', color:'green'},
              {label:'Total Documents', value:compact(s.total_documents), icon:'fileText', color:'blue',
                sub: s.observed_documents_total != null ? fmtFull(s.observed_documents_total) + ' seen in retrieval'
                  : s.total_documents == null ? null : 'Inventory reported by the indexers'},
              {label:'Total Chunks', value:compact(s.total_chunks), icon:'layers', color:'cyan',
                sub: s.observed_chunks_total != null ? fmtFull(s.observed_chunks_total) + ' seen in retrieval'
                  : s.total_chunks == null ? null : 'Inventory reported by the indexers'},
              {label:'Avg. Grounding Score', value:score(s.avg_grounding_score), icon:'trendUp', color:'orange',
                sub: s.sources_scored == null ? null : `${fmtFull(s.sources_scored)} sources scored`},
              {label:'Sources with ACL', value:num(s.sources_with_acl),
                sub: s.acl_percent == null ? null : pct(s.acl_percent, 0) + ' of total', icon:'shield', color:'amber'},
            ]);
          })
          .catch(err => fail(host, err, loadSummary, 'the knowledge summary'));
      }
      loadSummary();

      const table = dataTable({
        rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'sources',
        searchPlaceholder:'Search by name, index, location…',
        defaultSort:{ key:'updated_at', dir:-1 },
        emptyText:'No knowledge sources in this workspace yet',
        columns:[
          {key:'name', label:'Source Name', render:r=>`<div class="entity-cell"><span class="entity-ico" style="background:var(--panel-3)">${LOGOS[KS_LOGO[r.source_type]] || LOGOS.custom}</span>
            <div style="min-width:0"><div class="cell-main">${esc(r.name)}</div><div class="cell-sub">${esc(clip((r.settings && r.settings.description) || (r.settings && r.settings.location) || '', 40))}</div></div></div>`},
          {key:'source_type', label:'Type', render:r=>r.source_type?badge(r.source_type,'gray'):dash},
          {key:'environment', label:'Environment', render:r=>r.environment?badge(r.environment):dash},
          {key:'status', label:'Status', render:r=>r.status?statusText(r.status, ksStatusColor(r.status)):dash},
          {key:'document_count', label:'Documents', align:'right', cls:'num', render:r=>num(r.document_count) + seenLine(r, 'observed_documents')},
          {key:'chunk_count', label:'Chunks', align:'right', cls:'num', render:r=>num(r.chunk_count) + seenLine(r, 'observed_chunks')},
          {key:'grounding_score', label:'Grounding Score', render:r=>r.grounding_score == null ? dash
            : barPct(r.grounding_score * 100, r.grounding_score >= 0.85 ? 'green' : r.grounding_score >= 0.75 ? 'amber' : 'red', score(r.grounding_score))},
          {key:'sensitivity', label:'Sensitivity', render:r=>r.sensitivity?badge(r.sensitivity):dash},
          {key:'last_sync_at', label:'Last Sync', render:r=>r.status === 'Syncing'
            ? `<span class="st-amber nowrap">Syncing… <span class="small">${r.sync_progress == null ? '' : r.sync_progress + '%'}</span></span>`
            : (r.status === 'Failed' || r.status === 'Error') ? `<span class="st-red">${esc(r.status)}</span>`
            : `<span class="dim nowrap">${rel(r.last_sync_at)}</span>`},
          {key:'owner', label:'Owner', render:r=>r.owner_name?ownerCell(r.owner_name, r.owner_team||''):dash},
        ],
        filters:[
          {key:'type', label:'Type', param:'type', options:KS_TYPES, allLabel:'All Types'},
          {key:'status', label:'Status', param:'status', options:KS_STATUSES, allLabel:'All Status'},
          {key:'env', label:'Environment', param:'env', options:ENVIRONMENTS, allLabel:'All Environments'},
          {key:'sensitivity', label:'Sensitivity', param:'sensitivity', options:SENSITIVITIES, allLabel:'All Sensitivity'},
        ],
        source: (params) => API.knowledge.list(params),
        exportSource: (params) => API.knowledge.export(params),
        autoSelectFirst: true,
        onSelect: showSource,
        onLoad: (rows) => rows.forEach(r=>{ if(r.status === 'Syncing') watchSync(r.id); }),
        rowActions: r=>[
          roleItem('operator','Syncing a source', {label:'Sync Now', icon:'refresh', onClick:()=>syncSource(r)}),
          {label:'View Documents', icon:'fileText', onClick:()=>viewDocs(r)},
          roleItem('operator','Editing a source', {label:'Edit Source', icon:'edit', onClick:()=>editSource(r)}),
          {sep:true},
          roleItem('admin','Deleting a source', {label:'Delete Source', icon:'trash', danger:true, onClick:()=>deleteSource(r)}),
        ],
      });

      const wrap = document.getElementById('ksTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);
      document.getElementById('ksSearch').addEventListener('input', e=>table.search(e.target.value));
      document.getElementById('ksExport').addEventListener('click', ()=>table.export());

      // Warm the owner pickers for the people who can open them (the directory
      // opens at operator). A dialog asks again, out loud, if this did not land.
      if(Store.session.can('operator')) owners.load().catch(()=>{});

      function refreshAll(){ table.refresh(); loadSummary(); }

      /* ---- the alternate tabs, each its own server-side view of /knowledge ---- */
      let altTable = null;
      function clearAlt(){
        const alt = document.getElementById('ksAlt');
        if(alt) alt.remove();
        altTable = null;
      }

      tabBar(document.getElementById('ksTabs'), KS_TABS.map(t=>({label:t})), (i)=>{
        clearAlt();
        const primary = i === 0;
        table.el.style.display = primary ? '' : 'none';
        table.filterEl.style.display = primary ? '' : 'none';
        document.getElementById('ksLayout').classList.toggle('collapsed', !primary);
        if(primary) return;
        const div = document.createElement('div');
        div.id = 'ksAlt';
        wrap.appendChild(div);
        if(i === 1) altTable = indexTable(div);
        else if(i === 2) altTable = policyTable(div);
        else if(i === 3) groundingPanel(div);
        else altTable = lineageTable(div);
      });

      function indexTable(host){
        const t = dataTable({
          rowId:'id', pageSize:10, itemName:'indexes', emptyText:'No vector indexes — add a knowledge source first',
          columns:[
            {key:'name', label:'Index Name', render:r=>mono(r.index_name || '—')},
            {key:'source_type', label:'Backend', render:r=>dim(r.source_type)},
            {key:'embedding_model', label:'Embedding Model', sortable:false, render:r=>dim(r.embedding_model)},
            {key:'chunk_count', label:'Vectors', align:'right', cls:'num', render:r=>num(r.chunk_count) + seenLine(r, 'observed_chunks')},
            {key:'document_count', label:'Documents', align:'right', cls:'num', render:r=>num(r.document_count) + seenLine(r, 'observed_documents')},
            {key:'chunk_size', label:'Chunk / Overlap', sortable:false, render:r=>r.chunk_size == null ? dash : `<span class="dim">${r.chunk_size} / ${r.chunk_overlap == null ? '—' : r.chunk_overlap}</span>`},
            {key:'indexing', label:'Freshness', sortable:false, render:r=>r.indexing_status?statusText(r.indexing_status, r.indexing_status === 'Up to date' ? 'green' : r.indexing_status === 'Failed' ? 'red' : 'amber'):dash},
            {key:'status', label:'Status', render:r=>r.status?statusText(r.status, ksStatusColor(r.status)):dash},
          ],
          source: (params) => API.knowledge.list(params),
        });
        host.appendChild(t.el);
        return t;
      }

      function policyTable(host){
        const t = dataTable({
          rowId:'id', pageSize:10, itemName:'retrieval policies', emptyText:'No retrieval policies — add a knowledge source first',
          columns:[
            {key:'policy', label:'Policy', sortable:false, render:r=>`<b>${esc((r.retrieval_policy && r.retrieval_policy.name) || 'Default')}</b>`},
            {key:'name', label:'Applies To', render:r=>esc(r.name)},
            {key:'top_k', label:'Top-K', align:'right', cls:'num', sortable:false, render:r=>num(r.retrieval_policy && r.retrieval_policy.top_k)},
            {key:'min_score', label:'Min Score', align:'right', cls:'num', sortable:false, render:r=>score(r.retrieval_policy && r.retrieval_policy.min_score)},
            {key:'acl', label:'ACL Enforcement', sortable:false, render:r=>dim(r.retrieval_policy && r.retrieval_policy.acl_enforcement)},
            {key:'reranker', label:'Reranker', sortable:false, render:r=>dim(r.retrieval_policy && r.retrieval_policy.reranker)},
            {key:'status', label:'Status', render:r=>r.status?statusText(r.status, ksStatusColor(r.status)):dash},
          ],
          source: (params) => API.knowledge.list(params),
        });
        host.appendChild(t.el);
        return t;
      }

      function lineageTable(host){
        const t = dataTable({
          rowId:'id', pageSize:10, itemName:'sources', emptyText:'No sources to trace yet',
          columns:[
            {key:'name', label:'Source', render:r=>`<b>${esc(r.name)}</b>`},
            {key:'sensitivity', label:'Sensitivity', render:r=>r.sensitivity?badge(r.sensitivity):dash},
            {key:'acl', label:'ACL Status', sortable:false, render:r=>r.has_acl?badge('ACL Enforced','green'):badge('Not enforced','amber')},
            {key:'acl_summary', label:'Reader Principal', sortable:false, render:r=>dim(r.acl_summary)},
            {key:'lineage', label:'Lineage', sortable:false, render:r=>`<span class="dim">${esc(r.source_type || '—')} → chunker → ${esc(r.embedding_model || 'embeddings')} → ${esc(r.index_name || '—')}</span>`},
            {key:'last_sync_at', label:'Last Sync', render:r=>`<span class="dim nowrap">${rel(r.last_sync_at)}</span>`},
          ],
          source: (params) => API.knowledge.list(params),
        });
        host.appendChild(t.el);
        return t;
      }

      function groundingPanel(host){
        host.innerHTML = `<div class="card"><div class="card-head"><div class="card-title">Citations & Grounding</div>
            <select class="filter-select" id="gsPick" style="min-width:240px;height:30px"><option>Loading sources…</option></select></div>
          <div id="gsBody">${LOADING(180)}</div></div>`;
        const picker = host.querySelector('#gsPick');
        const body = host.querySelector('#gsBody');
        API.knowledge.list({ page_size: 50, sort:'name' })
          .then(page => {
            const items = page.items || [];
            if(!items.length){
              picker.innerHTML = '<option>No sources</option>';
              body.innerHTML = EMPTY('book','No knowledge sources yet','Add a source and let it sync before grounding can be measured.');
              return;
            }
            picker.innerHTML = items.map(s=>`<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
            picker.addEventListener('change', ()=>loadGrounding(picker.value));
            loadGrounding(items[0].id);
          })
          .catch(err => { picker.innerHTML = '<option>Unavailable</option>'; fail(body, err, ()=>groundingPanel(host), 'the source list'); });

        function loadGrounding(id){
          body.innerHTML = LOADING(180);
          API.knowledge.grounding(id)
            .then(g => {
              if(!g.measured){
                body.innerHTML = EMPTY('target','Grounding has not been measured for this source',
                  `The telemetry engine sampled ${g.spans_sampled == null ? 0 : g.spans_sampled} retrieval spans across ${g.projects_scanned == null ? 0 : g.projects_scanned} of ${g.projects_total == null ? 0 : g.projects_total} projects in the last ${g.window_days || 30} days.`);
                return;
              }
              body.innerHTML = `<div class="donut-wrap">${gaugeRing((g.overall || 0) * 100, 'orange', 130, 'Grounding')}
                  <div class="legend grow">${(g.dimensions || []).map(d=>
                    `<div class="legend-item"><span class="sw" style="background:#EA580C"></span>
                      <span class="lg-label">${esc(d.label || d.name)}</span>
                      <span class="lg-val sc-good">${score(d.score)}</span>
                      <span class="lg-pct">${d.sample_size == null ? '' : fmtFull(d.sample_size) + ' spans'}</span></div>`).join('')}</div></div>
                <div class="small faint" style="margin-top:8px">${fmtFull(g.spans_sampled || 0)} retrieval spans sampled across ${g.projects_scanned || 0} of ${g.projects_total || 0} projects · last ${g.window_days || 30} days</div>`;
            })
            .catch(err => fail(body, err, ()=>loadGrounding(id), 'the grounding breakdown'));
        }
      }

      /* ------------------------ the real sync job ------------------------ */

      /**
       * Follow one source's sync by polling the server's own job state. The
       * percentage on screen is the job's percentage — nothing here counts a
       * timer up to 100.
       */
      /* Each poll is scheduled by the answer to the one before it, so a slow
         answer cannot overlap the next and announce one completion twice. One
         failed poll is not the end of the job either: a 429, a timeout or a
         network blip used to stop the watch for good, and the row then read
         "Syncing… 40%" until the page was reloaded. Failures back off (4 s, 8 s,
         16 s) and only the fourth in a row gives up — at once when the answer
         says the source is gone or the session is. Giving up re-reads the
         table, which re-arms the watch if the row is still syncing. */
      const SYNC_POLL_MS = 2000, SYNC_POLL_TRIES = 4;
      function watchSync(id){
        if(pollers[id]) return;
        const watch = pollers[id] = { timer: null, failures: 0 };
        const again = (ms) => { if(pollers[id] === watch) watch.timer = setTimeout(poll, ms); };
        function poll(){
          /* Nobody is watching a hidden tab's progress bar, so skip the read
             and look again on the next tick. The watch stays armed, so it
             catches up the moment the tab comes back — unlike a bare return,
             which would strand the row at "Syncing… 40%". */
          if(document.hidden){ again(SYNC_POLL_MS); return; }
          API.knowledge.syncStatus(id)
            .then(st => {
              if(pollers[id] !== watch) return;
              watch.failures = 0;
              if(current && current.id === id) paintSyncState(st);
              if(st.running){ again(SYNC_POLL_MS); return; }
              stopWatch(id);
              table.refresh();
              loadSummary();
              if(current && current.id === id) showSource({ id });
              if(st.status === 'Failed' || st.error){
                toast('error','Sync failed', `${st.name || 'Source'} — ${st.error || 'the job did not complete.'}`);
              } else {
                toast('success','Sync complete',
                  `${st.name || 'Source'} — ${fmtFull(st.documents_seen || 0)} documents, ${fmtFull(st.chunks_seen || 0)} chunks.`);
              }
            })
            .catch(err => {
              if(pollers[id] !== watch) return;
              watch.failures += 1;
              const final = err && (err.status === 401 || err.status === 403 || err.status === 404);
              if(!final && watch.failures < SYNC_POLL_TRIES){
                again(SYNC_POLL_MS * Math.pow(2, watch.failures));
                return;
              }
              stopWatch(id);
              if(current && current.id === id) paintSyncLost(id, err);
              table.refresh();
              loadSummary();
            });
        }
        again(SYNC_POLL_MS);
      }
      function stopWatch(id){
        const watch = pollers[id];
        if(!watch) return;
        clearTimeout(watch.timer);
        delete pollers[id];
      }
      function stopAllWatches(){ Object.keys(pollers).forEach(stopWatch); }
      this.cleanup = () => { stopAllWatches(); clearAlt(); };

      /* The bar owns everything in the inspector that a running job changes.
         It is rendered hidden for a source that is not syncing, and nothing
         un-hid it when the sync was started from this screen — the stage and
         percentage were written into an invisible box, the header went on
         saying Active, and Sync Now stayed live for a second click to 409. */
      function paintSyncState(st){
        const host = document.getElementById('ksSyncBar');
        if(!host) return;
        host.style.display = '';
        if(st.running){
          const chip = document.getElementById('ksStatus');
          if(chip) chip.innerHTML = statusText('Syncing', ksStatusColor('Syncing'));
          const btn = document.getElementById('ksSync');
          if(btn && !btn.disabled){
            btn.disabled = true;
            btn.title = 'A sync is already running for this source.';
          }
        }
        const p = st.progress == null ? 0 : st.progress;
        host.innerHTML = `<div class="small" style="margin-bottom:4px">${esc(st.stage || 'Working')} ${st.stage_index != null && st.stage_count ? `(${st.stage_index} of ${st.stage_count})` : ''}</div>
          ${barPct(p, p >= 100 ? 'green' : 'amber', p + '%')}
          <div class="small faint" style="margin-top:4px">${fmtFull(st.documents_seen || 0)} documents · ${fmtFull(st.chunks_seen || 0)} chunks seen${st.error?` · <span class="st-red">${esc(st.error)}</span>`:''}</div>`;
      }

      function paintSyncLost(id, err){
        const host = document.getElementById('ksSyncBar');
        if(!host) return;
        host.style.display = '';
        host.innerHTML = `<div class="small st-amber">${ICONS.alert} Lost contact with the sync job — ${esc(msgOf(err))}</div>
          <button class="btn sm" id="ksSyncRetry" style="margin-top:6px">${ICONS.refresh}Check again</button>`;
        host.querySelector('#ksSyncRetry').addEventListener('click', ()=>{
          host.innerHTML = LOADING(50);
          watchSync(id);
        });
      }

      async function syncSource(r){
        if(!allowed('operator','Syncing a knowledge source')) return;
        try {
          const res = await API.knowledge.sync(r.id);
          const st = (res && res.sync) || res || {};
          toast('info','Sync started', `${r.name} — ${st.stage || 'queued'}.`);
          table.refresh();
          watchSync(r.id);
          if(current && current.id === r.id) paintSyncState(st);
        } catch (err) {
          toast('error','Could not start the sync', msgOf(err));
        }
      }

      /* ------------------------------ mutations ------------------------------ */

      document.getElementById('ksAdd').addEventListener('click', ()=>{
        if(!allowed('operator','Adding a knowledge source')) return;
        openModal({
          title:'Add Knowledge Source', icon:'book', wide:true,
          body:`<div class="form-row"><label>SOURCE NAME</label><input class="input" id="asName" placeholder="e.g. Compliance Hub"></div>
            <div class="grid g2">
              <div class="form-row"><label>TYPE</label><select class="filter-select w-100" id="asType" style="height:34px">${optionList(KS_TYPES,'SharePoint')}</select></div>
              <div class="form-row"><label>SENSITIVITY</label><select class="filter-select w-100" id="asSens" style="height:34px">${optionList(SENSITIVITIES,'Internal')}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="asEnv" style="height:34px">${optionList(ENVIRONMENTS,'Production')}</select></div>
              <div class="form-row"><label>OWNER</label><select class="filter-select w-100" id="asOwner" style="height:34px">${owners.options(me().id, me().full_name)}</select></div>
            </div>
            <div class="form-row"><label>LOCATION / URL</label><input class="input" id="asLoc" placeholder="URL or path"></div>
            <div class="form-row"><label>DESCRIPTION</label><input class="input" id="asDesc" placeholder="What does this source contain?"></div>
            <div class="grid g2">
              <div class="form-row"><label>INDEX NAME</label><input class="input" id="asIndex" placeholder="e.g. compliance-hub-v1"></div>
              <div class="form-row"><label>EMBEDDING MODEL</label><input class="input" id="asModel" placeholder="e.g. text-embedding-3-large"></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>CHUNK SIZE</label><input class="input" id="asChunk" type="number" value="512"></div>
              <div class="form-row"><label>CHUNK OVERLAP</label><input class="input" id="asOverlap" type="number" value="64"></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>DOCUMENTS (REPORTED BY THE INDEXER)</label><input class="input" id="asDocs" type="number" min="0" placeholder="Leave blank if not known"></div>
              <div class="form-row"><label>CHUNKS (REPORTED BY THE INDEXER)</label><input class="input" id="asChunks" type="number" min="0" placeholder="Leave blank if not known"></div>
            </div>
            <div class="form-row"><label>ACL DETAIL</label><input class="input" id="asAclSum" placeholder="Who may read it, e.g. the reader principal or group"></div>
            <label class="flex small" style="gap:7px;align-items:center"><input type="checkbox" id="asAcl" checked> ACL-aware crawling</label>
            <label class="flex small" style="gap:7px;align-items:center;margin-top:6px"><input type="checkbox" id="asSync" checked> Start the first sync now</label>`,
          footer:[
            {label:'Cancel'},
            {label:'Add Source', cls:'primary', onClick: async (close, modal) => {
              const name = (modal.querySelector('#asName').value || '').trim();
              if(!name){ toast('error','Name required','Give the source a name.'); return; }
              const chunk = parseInt(modal.querySelector('#asChunk').value, 10);
              const overlap = parseInt(modal.querySelector('#asOverlap').value, 10);
              const body = {
                name,
                source_type: modal.querySelector('#asType').value,
                environment: modal.querySelector('#asEnv').value,
                sensitivity: modal.querySelector('#asSens').value,
                has_acl: modal.querySelector('#asAcl').checked,
                owner_user_id: modal.querySelector('#asOwner').value || null,
                index_name: (modal.querySelector('#asIndex').value || '').trim() || null,
                embedding_model: (modal.querySelector('#asModel').value || '').trim() || null,
                chunk_size: isNaN(chunk) ? null : chunk,
                chunk_overlap: isNaN(overlap) ? null : overlap,
                settings: {
                  description: (modal.querySelector('#asDesc').value || '').trim() || null,
                  location: (modal.querySelector('#asLoc').value || '').trim() || null,
                },
                start_sync: modal.querySelector('#asSync').checked,
              };
              // A blank inventory is "not reported": the key is left out, never
              // sent as null — the columns cannot hold one.
              const docs = parseInt(modal.querySelector('#asDocs').value, 10);
              const chunks = parseInt(modal.querySelector('#asChunks').value, 10);
              if(!isNaN(docs)) body.document_count = docs;
              if(!isNaN(chunks)) body.chunk_count = chunks;
              const aclDetail = (modal.querySelector('#asAclSum').value || '').trim();
              if(aclDetail) body.acl_summary = aclDetail;
              try {
                const res = await Store.mutate(() => API.knowledge.create(body), { event:'knowledge:changed' });
                close();
                const src = res.source || res;
                toast('success','Source added', res.message || `${src.name} registered.`);
                refreshAll();
                if(body.start_sync && src.id) watchSync(src.id);
              } catch (err) {
                toast('error','Could not add the source', msgOf(err));
              }
            }},
          ],
          onOpen(modal){ owners.attach(modal.querySelector('#asOwner'), me().id, me().full_name); },
        });
      });

      function editSource(r){
        if(!allowed('operator','Editing a knowledge source')) return;
        const rp = r.retrieval_policy || {};
        const st = r.settings || {};
        openModal({
          title:'Edit Source — ' + r.name, icon:'edit', wide:true,
          body:`<div class="form-row"><label>SOURCE NAME</label><input class="input" id="esName" value="${esc(r.name || '')}"></div>
            <div class="grid g2">
              <div class="form-row"><label>TYPE</label><select class="filter-select w-100" id="esType" style="height:34px">${optionList(KS_TYPES, r.source_type)}</select></div>
              <div class="form-row"><label>SENSITIVITY</label><select class="filter-select w-100" id="esSens" style="height:34px">${optionList(SENSITIVITIES, r.sensitivity)}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="esEnv" style="height:34px">${optionList(ENVIRONMENTS, r.environment)}</select></div>
              <div class="form-row"><label>OWNER</label><select class="filter-select w-100" id="esOwner" style="height:34px">${owners.options(r.owner_user_id, r.owner_name)}</select></div>
            </div>
            <div class="form-row"><label>LOCATION / URL</label><input class="input" id="esLoc" value="${esc(st.location || '')}"></div>
            <div class="form-row"><label>DESCRIPTION</label><input class="input" id="esDesc" value="${esc(st.description || '')}"></div>
            <div class="grid g2">
              <div class="form-row"><label>RETRIEVAL TOP-K</label><input class="input" id="esTopK" type="number" value="${rp.top_k == null ? '' : rp.top_k}"></div>
              <div class="form-row"><label>MIN SCORE</label><input class="input" id="esMin" type="number" step="0.01" value="${rp.min_score == null ? '' : rp.min_score}"></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>RETRIEVAL POLICY NAME</label><input class="input" id="esPolicy" value="${esc(rp.name || '')}"></div>
              <div class="form-row"><label>RERANKER</label><input class="input" id="esRerank" value="${esc(rp.reranker || '')}"></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>ACL ENFORCEMENT</label><select class="filter-select w-100" id="esAclMode" style="height:34px">${optionListKeeping(ACL_ENFORCEMENTS, rp.acl_enforcement)}</select></div>
              <div class="form-row"><label>ACL DETAIL</label><input class="input" id="esAclSum" value="${esc(r.acl_summary || '')}"></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>DOCUMENTS (REPORTED BY THE INDEXER)</label><input class="input" id="esDocs" type="number" min="0" value="${r.document_count == null ? '' : r.document_count}"></div>
              <div class="form-row"><label>CHUNKS (REPORTED BY THE INDEXER)</label><input class="input" id="esChunks" type="number" min="0" value="${r.chunk_count == null ? '' : r.chunk_count}"></div>
            </div>
            <label class="flex small" style="gap:7px;align-items:center"><input type="checkbox" id="esAcl" ${r.has_acl?'checked':''}> ACL-aware crawling</label>`,
          footer:[
            {label:'Cancel'},
            {label:'Save Changes', cls:'primary', onClick: async (close, modal) => {
              const topK = parseInt(modal.querySelector('#esTopK').value, 10);
              const minScore = parseFloat(modal.querySelector('#esMin').value);
              const body = {
                name: (modal.querySelector('#esName').value || '').trim(),
                source_type: modal.querySelector('#esType').value,
                sensitivity: modal.querySelector('#esSens').value,
                environment: modal.querySelector('#esEnv').value,
                has_acl: modal.querySelector('#esAcl').checked,
                // The server replaces the whole settings block, so overlay the
                // edits on the loaded values or the unedited keys are wiped.
                settings: Object.assign({}, st, {
                  description: (modal.querySelector('#esDesc').value || '').trim() || null,
                  location: (modal.querySelector('#esLoc').value || '').trim() || null,
                }),
                expected_updated_at: r.updated_at || null,
              };
              // The server replaces the whole policy block too, so it is sent
              // only when one of its fields was edited, overlaid on what was
              // loaded — `filters` and anything else this dialog does not show
              // must survive the save.
              const policy = {
                name: (modal.querySelector('#esPolicy').value || '').trim() || null,
                top_k: isNaN(topK) ? null : topK,
                min_score: isNaN(minScore) ? null : minScore,
                acl_enforcement: modal.querySelector('#esAclMode').value || null,
                reranker: (modal.querySelector('#esRerank').value || '').trim() || null,
              };
              if(Object.keys(policy).some(k => policy[k] !== (rp[k] == null ? null : rp[k]))){
                body.retrieval_policy = Object.assign({}, rp, policy);
              }
              // A blank inventory is left out, never sent as null: the columns
              // cannot hold one, and the refusal reads as a name conflict.
              const docs = parseInt(modal.querySelector('#esDocs').value, 10);
              const chunks = parseInt(modal.querySelector('#esChunks').value, 10);
              if(!isNaN(docs) && docs !== r.document_count) body.document_count = docs;
              if(!isNaN(chunks) && chunks !== r.chunk_count) body.chunk_count = chunks;
              const aclDetail = (modal.querySelector('#esAclSum').value || '').trim() || null;
              if(aclDetail !== (r.acl_summary || null)) body.acl_summary = aclDetail;
              // The owner travels only when the reader chose a different one. A
              // picker that failed to load must never be read as "Unassigned".
              const owner = owners.change(modal.querySelector('#esOwner'), r.owner_user_id);
              if(owner !== undefined) body.owner_user_id = owner;
              try {
                const saved = await Store.mutate(() => API.knowledge.update(r.id, body), { event:'knowledge:changed' });
                close();
                toast('success','Source updated', `${saved.name} saved.`);
                refreshAll();
                showSource(saved);
              } catch (err) {
                toast('error','Could not save the source', msgOf(err));
              }
            }},
          ],
          onOpen(modal){ owners.attach(modal.querySelector('#esOwner'), r.owner_user_id, r.owner_name); },
        });
      }

      function deleteSource(r){
        if(!allowed('admin','Deleting a knowledge source')) return;
        confirmModal({
          title:'Delete Knowledge Source', danger:true, confirmLabel:'Delete',
          body:`<p style="margin-top:0">Delete <b style="color:var(--text)">${esc(r.name)}</b>?</p>
            <p class="small">The index reference and ${r.chunk_count == null ? 'its' : fmtFull(r.chunk_count)} recorded chunks are removed, and agents using this source lose retrieval coverage. The audit row survives it.</p>`,
          onConfirm: async () => {
            try {
              await Store.mutate(() => API.knowledge.remove(r.id), { event:'knowledge:changed' });
              toast('success','Source deleted', `${r.name} removed.`);
              stopWatch(r.id);
              current = null;
              document.getElementById('ksLayout').classList.add('collapsed');
              refreshAll();
            } catch (err) {
              toast('error','Could not delete the source', msgOf(err));
            }
          },
        });
      }

      function viewDocs(r){
        openModal({
          title:'Documents — ' + r.name, icon:'fileText', wide:true,
          body:'<div id="kdHost"></div>', footer:[{label:'Close'}],
          onOpen(modal){
            const host = modal.querySelector('#kdHost');
            const t = dataTable({
              rowId:'document_id', pageSize:10, itemName:'documents',
              searchPlaceholder:'Search documents…',
              emptyText:'No documents were retrieved from this source in the window',
              columns:[
                {key:'title', label:'Document', sortable:false, render:r2=>`<b>${esc(r2.title || r2.document_id)}</b>`},
                {key:'chunks', label:'Chunks', align:'right', cls:'num', sortable:false, render:r2=>num(r2.chunks)},
                {key:'avg_retrieval_score', label:'Avg Retrieval Score', align:'right', cls:'num', sortable:false, render:r2=>score(r2.avg_retrieval_score)},
                {key:'retrieved_by', label:'Retrieved By', sortable:false, render:r2=>(r2.retrieved_by || []).length ? dim(r2.retrieved_by.join(', ')) : dash},
                {key:'last_retrieved_at', label:'Last Retrieved', sortable:false, render:r2=>`<span class="dim nowrap">${rel(r2.last_retrieved_at)}</span>`},
                {key:'sensitivity', label:'Sensitivity', sortable:false, render:r2=>r2.sensitivity?badge(r2.sensitivity):dash},
                {key:'status', label:'Status', sortable:false, render:r2=>r2.status?statusText(r2.status, 'green'):dash},
              ],
              source: (params) => API.knowledge.documents(r.id, params),
            });
            host.appendChild(t.filterEl || document.createComment('no filters'));
            host.appendChild(t.el);
          },
        });
      }

      /* ------------------------------ inspector ------------------------------ */

      function showSource(row){
        if(!row) return;
        current = row;
        const layout = document.getElementById('ksLayout');
        const insp = document.getElementById('ksInspector');
        if(!layout || !insp) return;
        layout.classList.remove('collapsed');
        insp.innerHTML = `<div class="insp-head"><div class="grow"><div class="insp-title">Knowledge Source</div>
            <div class="insp-sub">Loading…</div></div>
            <button class="icon-btn insp-close" id="ksClose">${ICONS.x}</button></div>${LOADING(220)}`;
        insp.querySelector('#ksClose').addEventListener('click', ()=>layout.classList.add('collapsed'));
        API.knowledge.get(row.id)
          .then(s => { if(current && current.id === row.id) paintSource(insp, s); })
          .catch(err => {
            if(!current || current.id !== row.id) return;
            const holder = insp.querySelector('.card-loading');
            if(holder) holder.replaceWith(screenError(err, ()=>showSource(row), 'this knowledge source'));
          });
      }

      function paintSource(insp, s){
        current = s;
        const st = s.settings || {}, rp = s.retrieval_policy || {}, job = s.sync || {};
        insp.innerHTML = `
          <div class="insp-head">
            <span class="entity-ico" style="width:38px;height:38px;background:var(--panel-3)">${LOGOS[KS_LOGO[s.source_type]] || LOGOS.custom}</span>
            <div class="grow"><div class="insp-title">${esc(s.name)}</div>
              <div class="flex" style="gap:6px;margin-top:4px"><span id="ksStatus">${s.status?statusText(s.status, ksStatusColor(s.status)):''}</span><span class="insp-sub">${esc(s.source_type || '')}</span></div></div>
            <button class="icon-btn insp-close" id="ksClose">${ICONS.x}</button></div>
          <div id="ksSyncBar" class="insp-section" ${s.status === 'Syncing' ? '' : 'style="display:none"'}></div>
          ${inspSection('Source Summary','info', kv([
            ['Type', text(s.source_type)],
            ['Location', st.location ? `<span class="small mono">${esc(st.location)}</span>` : dash],
            ['Description', `<span class="small dim right">${st.description ? esc(st.description) : '—'}</span>`],
            ['Environment', s.environment?badge(s.environment):dash],
            ['Owner', s.owner_name ? esc(s.owner_name) + (s.owner_team ? ` (${esc(s.owner_team)})` : '') : dash],
            ['Sensitivity', s.sensitivity?badge(s.sensitivity):dash],
            ['ACL', s.has_acl ? '<span class="st-green">Enforced</span>' : '<span class="st-amber">Not enforced</span>'],
            ['ACL Detail', text(s.acl_summary)],
            ['Documents (reported)', num(s.document_count)],
            ['Chunks (reported)', num(s.chunk_count)],
            ['Seen in Retrieval', st.observed_documents == null && st.observed_chunks == null ? dash
              : `${num(st.observed_documents)} documents · ${num(st.observed_chunks)} chunks`],
            ['Chunk Size', s.chunk_size == null ? dash : `${s.chunk_size} tokens${s.chunk_overlap == null ? '' : ` (overlap ${s.chunk_overlap})`}`],
            ['Indexing Errors', num(st.indexing_errors)],
          ]))}
          ${inspSection('Sync & Indexing','refresh', kv([
            ['Last Sync', when(s.last_sync_at)],
            ['Next Sync', when(s.next_sync_at)],
            ['Sync Frequency', st.sync_frequency_minutes == null ? dash : `Every ${st.sync_frequency_minutes} minutes`],
            ['Indexing Status', s.indexing_status
              ? (s.indexing_status === 'Up to date' ? '<span class="st-green">Up to date</span>' : `<span class="st-amber">${esc(s.indexing_status)}</span>`)
              : dash],
            ['Indexer', text(st.indexer)],
            ['Vector Index', mono(s.index_name)],
            ['Embedding Model', text(s.embedding_model)],
            ['Last Job', job.stage ? `${esc(job.stage)} (${esc(job.state || '')})` : dash],
            ['Job Error', job.error ? `<span class="st-red small">${esc(job.error)}</span>` : dash],
          ]))}
          ${inspSection('Retrieval Policy','filter', kv([
            ['Policy', text(rp.name)],
            ['Top-K', num(rp.top_k)],
            ['Min Score', score(rp.min_score)],
            ['ACL Enforcement', text(rp.acl_enforcement)],
            ['Reranker', text(rp.reranker)],
          ]))}
          <div id="ksGrounding">${inspSection('Grounding & Quality','target', LOADING(120))}</div>
          <div class="insp-section"><div class="insp-section-title">Quick Actions</div>
            <div class="grid g2" style="gap:8px">
              <button class="btn sm" id="ksSync">${ICONS.refresh}Sync Now</button>
              <button class="btn sm" id="ksEdit">${ICONS.edit}Edit Source</button>
              <button class="btn sm" id="ksDocs">${ICONS.fileText}View Documents</button>
              <button class="btn sm ghost-danger" id="ksDel">${ICONS.trash}Delete Source</button>
            </div></div>`;
        insp.querySelector('#ksClose').addEventListener('click', ()=>document.getElementById('ksLayout').classList.add('collapsed'));
        const syncBtn = requireRole(insp.querySelector('#ksSync'), 'operator', 'Syncing a knowledge source');
        if(s.status === 'Syncing' && !syncBtn.disabled){
          syncBtn.disabled = true;
          syncBtn.title = 'A sync is already running for this source.';
        }
        syncBtn.addEventListener('click', ()=>syncSource(s));
        requireRole(insp.querySelector('#ksEdit'), 'operator', 'Editing a knowledge source')
          .addEventListener('click', ()=>editSource(s));
        insp.querySelector('#ksDocs').addEventListener('click', ()=>viewDocs(s));
        requireRole(insp.querySelector('#ksDel'), 'admin', 'Deleting a knowledge source')
          .addEventListener('click', ()=>deleteSource(s));

        if(s.status === 'Syncing'){
          const bar = insp.querySelector('#ksSyncBar');
          if(bar){ bar.style.display = ''; bar.innerHTML = LOADING(50); }
          watchSync(s.id);
        }
        loadGroundingSection(s);
      }

      /* The grounding scan is the slowest read on this screen, so its answer can
         arrive after the reader has moved to another source — and used to paint
         source A's gauge into source B's panel. Only the answer for the source
         still on show is kept. */
      let groundingFor = null;
      function loadGroundingSection(s){
        const host = document.getElementById('ksGrounding');
        if(!host) return;
        groundingFor = s.id;
        API.knowledge.grounding(s.id)
          .then(g => {
            if(groundingFor !== s.id || !document.getElementById('ksGrounding')) return;
            host.innerHTML = inspSection('Grounding & Quality','target', g.measured
              ? `<div class="donut-wrap">${gaugeRing((g.overall || 0) * 100, 'orange', 96, 'Grounding')}
                  <div class="legend grow">${(g.dimensions || []).map(d=>
                    `<div class="legend-item"><span class="sw" style="background:#EA580C"></span>
                      <span class="lg-label" style="font-size:11.5px">${esc(d.label || d.name)}</span>
                      <span class="lg-val sc-good">${score(d.score)}</span></div>`).join('')}</div></div>
                 <div class="small faint" style="margin-top:6px">${fmtFull(g.spans_sampled || 0)} retrieval spans sampled · last ${g.window_days || 30} days</div>`
              : EMPTY('target','Not measured yet',
                  `No retrieval spans for this source were found in the last ${g.window_days || 30} days.`));
          })
          .catch(err => {
            if(groundingFor !== s.id || !document.getElementById('ksGrounding')) return;
            host.innerHTML = '';
            host.appendChild(screenError(err, ()=>loadGroundingSection(s), 'the grounding breakdown'));
          });
      }
    },
  };

  /* ====================== SECRETS & CREDENTIALS ====================== */

  const SECRET_TYPES = ['API Key','Service Principal','OAuth Client','Certificate','Connector Credential'];
  const SECRET_STATUSES = ['Active','Expiring Soon','Warning','Rotation Overdue','Expired','Disabled','Revoked'];
  const SECRET_RISKS = ['Low','Medium','High'];
  const SC_KPIS = ['Total Secrets','Active Vaults','Expiring Soon','Rotation Overdue','Compliance Score','Privileged Access (30d)'];
  const SC_TABS = [
    ['All Secrets', null], ['API Keys','API Key'], ['OAuth Clients','OAuth Client'],
    ['Certificates','Certificate'], ['Service Principals','Service Principal'],
    ['Connector Credentials','Connector Credential'],
  ];

  function secretStatusColor(s){
    if(s === 'Active') return 'green';
    if(s === 'Expiring Soon' || s === 'Warning' || s === 'Rotation Overdue') return 'amber';
    if(s === 'Expired' || s === 'Revoked') return 'red';
    return 'gray';
  }

  SCREENS['secrets'] = {
    title:'Secrets & Credentials',
    render(main){
      let current = null, vaults = [];
      const owners = ownerDirectory(() => API.secrets.owners());

      main.innerHTML = `
        ${pageHead({title:'Secrets & Credentials', sub:'Manage vaults, API keys, certificates, service principals, and credential rotation across your AI ecosystem.',
          actions:`${searchBox('scSearch','Search secrets…')}
          <button class="btn" id="scExport">${ICONS.download}Export</button>
          <button class="btn primary" id="scAdd">${ICONS.plus}Add Secret</button>`})}
        <div id="scKpis">${kpiSkeleton(SC_KPIS)}</div>
        <div id="scTabs"></div>
        <div class="with-inspector" id="scLayout">
          <div id="scTableWrap"></div>
          <div class="inspector" id="scInspector">
            <div class="insp-head"><div><div class="insp-title">Secret</div>
              <div class="insp-sub">Select a row to inspect it.</div></div></div>
          </div>
        </div>
        <div class="grid g4 mt" id="scPanels">
          <div class="card">${LOADING(150)}</div><div class="card">${LOADING(150)}</div>
          <div class="card">${LOADING(150)}</div><div class="card">${LOADING(150)}</div>
        </div>`;

      requireRole(document.getElementById('scAdd'), 'admin', 'Storing a secret');

      /* `panels:false` repaints the KPI row alone. The four panels below cost up
         to seven list calls, and a reveal moves none of their figures — only
         Privileged Access (30d). The skeleton is for the first paint; after
         that the old figures stay up until the new ones land, instead of the
         row blinking empty after every mutation. */
      let summaryPainted = false;
      function loadSummary(opts){
        const host = document.getElementById('scKpis');
        if(!host) return;
        const withPanels = !(opts && opts.panels === false);
        if(!summaryPainted) host.innerHTML = kpiSkeleton(SC_KPIS);
        API.secrets.summary()
          .then(s => {
            if(!document.getElementById('scKpis')) return;
            summaryPainted = true;
            host.innerHTML = kpiRow([
              {label:'Total Secrets', value:num(s.total), icon:'key', color:'purple',
                sub: (s.by_type || []).length ? `${s.by_type.length} credential types` : null},
              {label:'Active Vaults', value:num(s.active_vaults), sub:'Holding at least one live secret', icon:'lock', color:'blue'},
              {label:'Expiring Soon', value:`<span class="${s.expiring_soon?'st-amber':''}">${num(s.expiring_soon)}</span>`,
                sub:'Expires inside the 30-day window', icon:'clock', color:'amber'},
              {label:'Rotation Overdue', value:`<span class="${s.rotation_overdue?'st-red':''}">${num(s.rotation_overdue)}</span>`,
                sub: s.rotation_overdue ? 'Past the rotation deadline' : 'None', icon:'alert', color:'red'},
              // The score is taken over the credentials in service. With none, the
              // server answers 100 by convention — there is nothing to score, so
              // the card says that rather than showing a perfect mark.
              {label:'Compliance Score', value: s.in_service === 0 ? dash : pct(s.compliance_score, 0),
                sub: s.in_service == null ? 'Share of secrets inside their rotation policy'
                  : s.in_service === 0 ? 'No credential is in service to score'
                  : `Of the ${fmtFull(s.in_service)} in service, inside their policy`, icon:'shieldCheck', color:'green'},
              {label:'Privileged Access (30d)', value:num(s.privileged_access_30d),
                sub:'Successful reveals and rotations of privileged secrets', icon:'users', color:'orange'},
            ]);
            // Every vault in the workspace, when the summary carries them; the
            // rows of the pages already viewed (onLoad below) are the fallback.
            if(Array.isArray(s.vaults)){
              s.vaults.forEach(v => { if(v && vaults.indexOf(v) < 0) vaults.push(v); });
              fillOptions(table, 3, vaults);
            }
            if(withPanels) paintPanels(s);
          })
          .catch(err => {
            summaryPainted = false;
            fail(host, err, ()=>loadSummary(opts), 'the secret summary');
            const panels = document.getElementById('scPanels');
            if(panels && withPanels) fail(panels, err, ()=>loadSummary(opts), 'the vault panels');
          });
      }
      loadSummary();

      const table = dataTable({
        rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'secrets', selectable:true,
        searchPlaceholder:'Search by name, vault, reference…',
        defaultSort:{ key:'created_at', dir:-1 },
        emptyText:'No secrets stored in this workspace yet',
        columns:[
          {key:'name', label:'Secret Name', render:r=>entityCell(r.name, r.vault_reference || null, 'key',
            r.risk === 'High' ? 'red' : r.risk === 'Medium' ? 'amber' : 'green')},
          {key:'secret_type', label:'Type', render:r=>dim(r.secret_type)},
          {key:'vault', label:'Vault / Store', render:r=>dim(r.vault)},
          {key:'environment', label:'Environment', render:r=>r.environment?badge(r.environment):dash},
          {key:'status', label:'Status', render:r=>r.status?badge(r.status, secretStatusColor(r.status)):dash},
          // "Expiring soon" is about expires_at, not the rotation clock, so the
          // amber badge carries the expiry — it used to show the rotation label
          // ("83d") in amber, a deadline that was not the one at risk.
          {key:'rotation', label:'Rotation', render:r=>r.is_rotation_overdue ? badge('Overdue','red')
            : r.is_expiring_soon ? expiryBadge(r)
            : dim(r.rotation_label)},
          {key:'last_accessed_at', label:'Last Accessed', render:r=>`<span class="dim nowrap">${rel(r.last_accessed_at)}</span>`},
          {key:'owner', label:'Owner', render:r=>r.owner_name?ownerCell(r.owner_name, ''):dash},
          {key:'risk', label:'Risk', render:r=>r.risk?riskBadge(r.risk):dash},
        ],
        filters:[
          {key:'type', label:'Type', param:'type', options:SECRET_TYPES, allLabel:'All Types'},
          {key:'env', label:'Environment', param:'env', options:ENVIRONMENTS, allLabel:'All Environments'},
          {key:'status', label:'Status', param:'status', options:SECRET_STATUSES, allLabel:'All Statuses'},
          {key:'vault', label:'Vault', param:'vault', options:[], allLabel:'All Vaults'},
          {key:'risk', label:'Risk', param:'risk', options:SECRET_RISKS, allLabel:'All Risk'},
        ],
        source: (params) => API.secrets.list(params),
        exportSource: (params) => API.secrets.export(params),
        autoSelectFirst: true,
        onSelect: showSecret,
        onLoad: (rows) => {
          // The vault dropdown can only list the vaults this workspace uses.
          const seen = rows.map(r=>r.vault).filter(Boolean);
          seen.forEach(v => { if(vaults.indexOf(v) < 0) vaults.push(v); });
          fillOptions(table, 3, vaults);
        },
        rowActions: r=>[
          // A disabled or revoked credential is not rotated — the server answers 412.
          ...(r.status === 'Disabled' || r.status === 'Revoked' ? []
            : [roleItem('admin','Rotating a secret', {label:'Rotate Secret', icon:'refresh', onClick:()=>rotate(r)})]),
          {label:'View Audit Log', icon:'history', onClick:()=>auditLog(r)},
          roleItem('admin','Editing access', {label:'Edit Access', icon:'lock', onClick:()=>editAccess(r)}),
          {sep:true},
          // Disabling a revoked secret always 409s, so offer neither action there.
          ...(r.status === 'Disabled'
            ? [roleItem('admin','Enabling a secret', {label:'Enable Secret', icon:'checkCircle', onClick:()=>enable(r)})]
            : r.status === 'Revoked' ? []
            : [roleItem('admin','Disabling a secret', {label:'Disable Secret', icon:'xCircle', danger:true, onClick:()=>disable(r)})]),
          roleItem('admin','Deleting a secret', {label:'Delete Secret', icon:'trash', danger:true, onClick:()=>removeSecret(r)}),
        ],
      });

      const wrap = document.getElementById('scTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);
      document.getElementById('scSearch').addEventListener('input', e=>table.search(e.target.value));
      document.getElementById('scExport').addEventListener('click', ()=>table.export());

      // Warm the owner pickers for the people who can open them — every dialog
      // that carries one is admin-only. A dialog asks again, out loud, if this
      // did not land.
      if(Store.session.can('admin')) owners.load().catch(()=>{});

      tabBar(document.getElementById('scTabs'), SC_TABS.map(t=>({label:t[0]})),
        i => setFilter(table, 0, SC_TABS[i][1]));

      function refreshAll(){ table.refresh(); loadSummary(); }

      /* ---- the four vault panels, every figure a server answer ---- */
      function paintPanels(s){
        const host = document.getElementById('scPanels');
        if(!host) return;
        const byType = s.by_type || [];
        host.innerHTML = `
          <div class="card"><div class="card-head"><div class="card-title">Vault Overview</div></div>
            <div id="scVaultBody">${LOADING(120)}</div></div>
          <div class="card"><div class="card-head"><div class="card-title">Compliance by Secret Type</div></div>
            ${byType.length
              ? hbars(byType.map(t=>({ label:t.secret_type, value:t.compliance_pct == null ? 0 : t.compliance_pct,
                  color: (t.compliance_pct || 0) >= 90 ? 'green' : (t.compliance_pct || 0) >= 70 ? 'amber' : 'red',
                  display: t.compliance_pct == null ? '—' : t.compliance_pct + '%',
                  // The percentage is taken over the credentials in service, so the
                  // fraction beside it is too — "100%  2/5" read as a contradiction.
                  pct: `${t.compliant}/${t.in_service == null ? t.count : t.in_service}` })), {labelW:130})
              : EMPTY('shieldCheck','Nothing to score','No secrets are stored yet.')}</div>
          <div class="card"><div class="card-head"><div class="card-title">Expiring Secrets <span class="muted small">(Next 7 Days)</span></div></div>
            <div id="scExpiring">${LOADING(110)}</div></div>
          <div class="card"><div class="card-head"><div class="card-title">Rotation Overdue</div></div>
            <div id="scOverdue">${LOADING(110)}</div></div>`;

        /* The donut counts come from the server. A summary that carries
           `by_status` ([{status, count}]) draws it with no further request;
           until the summary does, it costs one filtered count per status. Five
           of the seven statuses are asked for — the stored status partitions
           the total, so what is left over is the other two, shown as one slice
           rather than bought with two more queries. Without it the arcs did
           not add up to the number in the middle. */
        const STATUS_COLORS = { 'Active':'green', 'Expiring Soon':'amber', 'Rotation Overdue':'red',
          'Expired':'red', 'Disabled':'gray', 'Revoked':'red', 'Warning':'amber', 'Warning / Revoked':'gray' };
        const wanted = ['Active','Expiring Soon','Rotation Overdue','Expired','Disabled'];
        const counts = Array.isArray(s.by_status)
          ? Promise.resolve(s.by_status.map(b => ({ status: b.status, count: b.count })))
          : Promise.all(wanted.map(st => API.secrets.list({ status: st, page_size: 1 })
              .then(p => ({ status: st, count: p.total || 0 })).catch(()=>({ status: st, count: null }))))
              .then(rows => {
                const rest = s.total == null || rows.some(r=>r.count == null) ? 0
                  : s.total - rows.reduce((sum, r) => sum + r.count, 0);
                return rest > 0 ? rows.concat([{ status:'Warning / Revoked', count: rest }]) : rows;
              });
        counts
          .then(rows => {
            const body = document.getElementById('scVaultBody');
            if(!body) return;
            const known = rows.filter(r=>r.count != null && r.count > 0);
            if(!known.length){
              body.innerHTML = EMPTY('lock','No secrets stored', `${s.active_vaults || 0} vaults are configured.`);
              return;
            }
            body.innerHTML = `<div class="donut-wrap">
                ${donut({ segments: known.map(r=>({ value:r.count, color:STATUS_COLORS[r.status] || 'gray' })),
                  size:110, thickness:13, centerVal:String(s.total == null ? '—' : s.total), centerLabel:'Secrets' })}
                <div class="legend grow">${known.map(r=>
                  `<div class="legend-item"><span class="sw" style="background:${U.cc(STATUS_COLORS[r.status] || 'gray')}"></span>
                    <span class="lg-label">${esc(r.status)}</span><span class="lg-val">${r.count}</span></div>`).join('')}</div></div>
              <div class="small faint" style="margin-top:6px">${s.active_vaults == null ? '—' : s.active_vaults} active vaults</div>`;
          });

        listInto('#scExpiring', { expiring_within_days: 7, page_size: 5, sort:'expires_at' },
          'Nothing expires in the next 7 days', expiryBadge);
        listInto('#scOverdue', { rotation_state:'overdue', page_size: 5, sort:'next_rotation_at' },
          'No secret is past its rotation deadline', () => badge('Overdue','red'));
      }

      /** How close a credential is to its expiry: red once past, amber before. */
      function expiryBadge(x){
        if(!x.expires_at) return dim(x.rotation_label);
        const left = until(x.expires_at);
        return left ? badge('Expires ' + left, 'amber') : badge('Expired', 'red');
      }

      function listInto(sel, params, emptyMsg, rowBadge){
        const host = document.querySelector(sel);
        if(!host) return;
        API.secrets.list(params)
          .then(page => {
            const target = document.querySelector(sel);
            if(!target) return;
            const items = page.items || [];
            target.innerHTML = items.length
              ? items.map(x=>`<div class="kv"><span class="k" style="color:var(--text)">${esc(x.name)}</span>
                  <span class="v">${rowBadge(x)}</span></div>`).join('')
                + (page.total > items.length ? `<div class="small faint" style="margin-top:6px">${page.total - items.length} more</div>` : '')
              : `<div class="faint small">${esc(emptyMsg)}</div>`;
          })
          .catch(err => {
            const target = document.querySelector(sel);
            if(target) target.innerHTML = `<div class="small st-red">${esc(msgOf(err))}</div>`;
          });
      }

      /* ------------------------------ mutations ------------------------------ */

      document.getElementById('scAdd').addEventListener('click', ()=>{
        if(!allowed('admin','Storing a secret')) return;
        openModal({
          title:'Add Secret', icon:'key', wide:true,
          body:`<div class="form-row"><label>SECRET NAME</label><input class="input" id="nsName" placeholder="e.g. Payments API Key"></div>
            <div class="grid g2">
              <div class="form-row"><label>TYPE</label><select class="filter-select w-100" id="nsType" style="height:34px">${optionList(SECRET_TYPES,'API Key')}</select></div>
              <div class="form-row"><label>VAULT</label><input class="input" id="nsVault" placeholder="e.g. Azure Key Vault" list="scVaultList">
                <datalist id="scVaultList">${vaults.map(v=>`<option>${esc(v)}</option>`).join('')}</datalist></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="nsEnv" style="height:34px">${optionList(ENVIRONMENTS,'Production')}</select></div>
              <div class="form-row"><label>OWNER</label><select class="filter-select w-100" id="nsOwner" style="height:34px">${owners.options(me().id, me().full_name, true)}</select></div>
            </div>
            <div class="form-row"><label>VALUE</label><input class="input" type="password" id="nsValue" placeholder="Leave blank only if you fill in Vault Reference below"></div>
            <div class="form-row"><label>VAULT REFERENCE</label><input class="input" id="nsRef" placeholder="e.g. @KeyVault(SecretUri=…)"></div>
            <div class="grid g2">
              <div class="form-row"><label>ROTATION PERIOD (DAYS)</label><input class="input" id="nsRot" type="number" value="90"></div>
              <div class="form-row"><label>RISK</label><select class="filter-select w-100" id="nsRisk" style="height:34px">${optionList(SECRET_RISKS,'Low')}</select></div>
            </div>
            <label class="flex small" style="gap:7px;align-items:center"><input type="checkbox" id="nsPriv"> Privileged credential (every access is flagged)</label>`,
          footer:[
            {label:'Cancel'},
            {label:'Store in Vault', cls:'primary', onClick: async (close, modal) => {
              const name = (modal.querySelector('#nsName').value || '').trim();
              if(!name){ toast('error','Name required','Give the secret a name.'); return; }
              // The server requires a vault, and either material to encrypt or a
              // reference to where the material lives. Say which, here, instead
              // of sending a body it can only refuse.
              if(!(modal.querySelector('#nsVault').value || '').trim()){
                toast('error','Vault required','Name the vault or store that holds this credential.'); return;
              }
              if(!modal.querySelector('#nsValue').value && !(modal.querySelector('#nsRef').value || '').trim()){
                toast('error','Value or reference required','Provide a value to encrypt, or a vault reference for a credential managed elsewhere.'); return;
              }
              const rot = parseInt(modal.querySelector('#nsRot').value, 10);
              const body = {
                name,
                secret_type: modal.querySelector('#nsType').value,
                vault: (modal.querySelector('#nsVault').value || '').trim() || null,
                environment: modal.querySelector('#nsEnv').value,
                value: modal.querySelector('#nsValue').value || null,
                rotation_period_days: isNaN(rot) ? null : rot,
                owner_user_id: modal.querySelector('#nsOwner').value || null,
                risk: modal.querySelector('#nsRisk').value,
                vault_reference: (modal.querySelector('#nsRef').value || '').trim() || null,
                privileged: modal.querySelector('#nsPriv').checked,
              };
              try {
                const created = await Store.mutate(() => API.secrets.create(body), { event:'secrets:changed' });
                close();
                toast('success','Secret stored', `${created.name} saved. The value never touches the console.`);
                refreshAll();
              } catch (err) {
                toast('error','Could not store the secret', msgOf(err));
              }
            }},
          ],
          onOpen(modal){ owners.attach(modal.querySelector('#nsOwner'), me().id, me().full_name, true); },
        });
      });

      function editAccess(r){
        if(!allowed('admin','Editing secret access')) return;
        openModal({
          title:'Edit Access — ' + r.name, icon:'lock',
          body:`<div class="grid g2">
              <div class="form-row"><label>OWNER</label><select class="filter-select w-100" id="eaOwner" style="height:34px">${owners.options(r.owner_user_id, r.owner_name)}</select></div>
              <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="eaEnv" style="height:34px">${optionListKeeping(ENVIRONMENTS, r.environment)}</select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>RISK</label><select class="filter-select w-100" id="eaRisk" style="height:34px">${optionListKeeping(SECRET_RISKS, r.risk)}</select></div>
              <div class="form-row"><label>ROTATION PERIOD (DAYS)</label><input class="input" id="eaRot" type="number" value="${r.rotation_period_days == null ? '' : r.rotation_period_days}"></div>
            </div>
            <div class="form-row"><label>VAULT REFERENCE</label><input class="input" id="eaRef" value="${esc(r.vault_reference || '')}"></div>
            <label class="flex small" style="gap:7px;align-items:center"><input type="checkbox" id="eaPriv" ${r.privileged?'checked':''}> Privileged credential</label>
            <div class="quote small">${ICONS.info} Access changes are written to this secret's audit log.</div>`,
          footer:[
            {label:'Cancel'},
            {label:'Save Access', cls:'primary', onClick: async (close, modal) => {
              const rot = parseInt(modal.querySelector('#eaRot').value, 10);
              /* Only what the reader changed is sent. The dialog used to PATCH
                 all six fields on every save, so a select that could not show
                 the stored value wrote its first entry back, a picker that had
                 not loaded cleared the owner, and the audit row claimed six
                 changes for an edit to one. */
              const body = {};
              const put = (key, value, was) => { if(value !== was) body[key] = value; };
              const owner = owners.change(modal.querySelector('#eaOwner'), r.owner_user_id);
              if(owner !== undefined) body.owner_user_id = owner;
              put('environment', modal.querySelector('#eaEnv').value || null, r.environment || null);
              put('risk', modal.querySelector('#eaRisk').value || null, r.risk || null);
              put('rotation_period_days', isNaN(rot) ? null : rot, r.rotation_period_days == null ? null : r.rotation_period_days);
              put('vault_reference', (modal.querySelector('#eaRef').value || '').trim() || null, r.vault_reference || null);
              put('privileged', modal.querySelector('#eaPriv').checked, Boolean(r.privileged));
              if(!Object.keys(body).length){
                close();
                toast('info','Nothing to save','No access setting was changed.');
                return;
              }
              body.expected_updated_at = r.updated_at || null;
              try {
                const saved = await Store.mutate(() => API.secrets.update(r.id, body), { event:'secrets:changed' });
                close();
                toast('success','Access updated', `${saved.name} — the change is in the audit log.`);
                refreshAll();
                showSecret(saved);
              } catch (err) {
                if(!(err && err.isConflict)){ toast('error','Could not update access', msgOf(err)); return; }
                /* The row moved under this dialog — a reveal, a rotation and the
                   status sweep all do that. Saving again from here can only be
                   refused again, so reopen on the row as it stands now. */
                try {
                  const fresh = await API.secrets.get(r.id);
                  close();
                  toast('warn','This secret changed while the dialog was open', 'It has been reloaded — reapply your edit and save again.', 6000);
                  showSecret(fresh);
                  editAccess(fresh);
                } catch (again) {
                  toast('error','Could not update access', msgOf(err));
                }
              }
            }},
          ],
          onOpen(modal){ owners.attach(modal.querySelector('#eaOwner'), r.owner_user_id, r.owner_name); },
        });
      }

      /** The reveal is a privileged read: it needs a reason and it is logged. */
      function reveal(r){
        if(!allowed('admin','Revealing a secret')) return;
        openModal({
          title:'Reveal Secret — ' + r.name, icon:'eye',
          body:`<p style="margin-top:0" class="small">The value is shown once and an access-log row is written naming you, the time and the reason below.</p>
            <div class="form-row"><label>JUSTIFICATION</label><textarea class="input" id="rvWhy" rows="2" placeholder="Why do you need the plaintext?"></textarea></div>
            <div id="rvOut"></div>`,
          footer:[
            {label:'Close'},
            {label:'Reveal', cls:'danger', close:false, onClick: async (close, modal) => {
              const out = modal.querySelector('#rvOut');
              const justification = (modal.querySelector('#rvWhy').value || '').trim();
              if(!justification){ toast('error','Reason required','A reveal must carry a justification.'); return; }
              out.innerHTML = LOADING(60);
              try {
                const res = await API.secrets.reveal(r.id, { justification });
                out.innerHTML = `<div class="small muted" style="font-weight:700;margin:9px 0 4px">VALUE (SHOWN ONCE)</div>
                  <div class="secret-val"><span class="sv" id="rvVal" style="user-select:all">${esc(res.value)}</span>
                    <button class="icon-btn" id="rvCopy" title="Copy">${ICONS.copy}</button></div>
                  <div class="small faint" style="margin-top:6px">Revealed by ${esc(res.revealed_by || '')} at ${when(res.revealed_at)} · access log ${esc(String(res.access_log_id || '').slice(0,12))}</div>`;
                const copyBtn = out.querySelector('#rvCopy');
                if(copyBtn) copyBtn.addEventListener('click', ()=>copyText(res.value, 'Secret value copied'));
                toast('warn','Secret revealed','The reveal is recorded in this secret’s access log.');
                table.refresh();
                loadSummary({ panels: false });
                // A reveal moves the row's updated_at and adds an access-log
                // entry. The inspector behind this dialog still held the row as
                // it was, so its Edit Access was refused as a conflict and its
                // Recent Access did not show the reveal that had just happened.
                if(current && current.id === r.id) showSecret(r);
              } catch (err) {
                fail(out, err, null, 'the secret value');
                toast('error','Could not reveal', msgOf(err));
              }
            }},
          ],
        });
      }

      /**
       * Rotation is a real multi-step flow: choose, confirm, then read the result.
       *
       * Two kinds of credential pass through it, and the dialog used to describe
       * neither. Where the server holds the material, IT mints the new
       * value and stores it encrypted — no vault generates anything, and nothing
       * is pushed to the systems that use it. Where only a reference is held
       * (has_material false), a blank value records a rotation that was carried
       * out in the vault itself: the clock restarts and nothing is generated.
       */
      function rotate(r){
        if(!allowed('admin','Rotating a secret')) return;
        const held = Boolean(r.has_material);
        const vault = r.vault || 'the vault';
        openModal({
          title:'Rotate Secret — ' + r.name, icon:'refresh', wide:true,
          body:`<div id="rtStep1">
              <div class="pipe" style="margin-bottom:12px">
                <div class="pipe-step"><div class="pipe-dot active">1</div><div class="pipe-body">
                  <div class="pipe-title"><span>Choose the new material</span></div>
                  <div class="pipe-sub">${held ? 'Supply a value, or let the server generate one.'
                    : `Rotate it in ${esc(vault)} first, then record it here — or supply a value to store one.`}</div></div></div>
                <div class="pipe-step"><div class="pipe-dot">2</div><div class="pipe-body">
                  <div class="pipe-title"><span>Rotate</span></div>
                  <div class="pipe-sub">The server writes the new version and restarts the rotation clock.</div></div></div>
                <div class="pipe-step"><div class="pipe-dot">3</div><div class="pipe-body">
                  <div class="pipe-title"><span>Hand the value over</span></div>
                  <div class="pipe-sub">${held ? 'A generated value is shown once, here, and never again.'
                    : 'Nothing is generated for a reference-only credential.'}</div></div></div>
              </div>
              <div class="form-row"><label>NEW VALUE</label><input class="input" type="password" id="rtValue" placeholder="${held ? 'Leave blank to have the server generate one'
                : esc('Leave blank to record a rotation performed in ' + vault)}"></div>
              <div class="grid g2">
                <div class="form-row"><label>ROTATION PERIOD (DAYS)</label><input class="input" id="rtDays" type="number" value="${r.rotation_period_days == null ? '' : r.rotation_period_days}"></div>
                <div class="form-row"><label>REASON</label><input class="input" id="rtReason" placeholder="Recorded in the access log"></div>
              </div>
              <div class="quote small">${ICONS.info} ${held
                ? 'The new value is stored encrypted here. Nothing is pushed anywhere: hand it to the systems that use this credential.'
                : esc(`${r.vault || 'The vault'} holds the material. Rotate it there, then record it here; nothing is generated or pushed by the server.`)}</div>
            </div>
            <div id="rtStep2"></div>`,
          footer:[
            {label:'Close'},
            {label:'Rotate Now', cls:'primary', close:false, onClick: async (close, modal) => {
              const out = modal.querySelector('#rtStep2');
              const days = parseInt(modal.querySelector('#rtDays').value, 10);
              const btns = modal.querySelectorAll('.modal-foot .btn');
              out.innerHTML = LOADING(70);
              try {
                const res = await Store.mutate(() => API.secrets.rotate(r.id, {
                  value: modal.querySelector('#rtValue').value || null,
                  rotation_period_days: isNaN(days) ? null : days,
                  reason: (modal.querySelector('#rtReason').value || '').trim() || null,
                }), { event:'secrets:changed' });
                modal.querySelector('#rtStep1').style.display = 'none';
                if(btns[1]) btns[1].style.display = 'none';
                out.innerHTML = `<div class="pipe" style="margin-bottom:12px">
                    ${['Choose the new material','Rotate', res.recorded_upstream ? 'Rotation recorded' : 'Hand the value over'].map(s=>
                      `<div class="pipe-step"><div class="pipe-dot done">${ICONS.check}</div><div class="pipe-body">
                        <div class="pipe-title"><span>${esc(s)}</span><span class="st-green small">Done</span></div></div></div>`).join('')}
                  </div>
                  ${kv([
                    ['Rotated at', when(res.rotated_at)],
                    ['Next rotation', when(res.next_rotation_at)],
                    ['Generated by the server', res.generated ? '<span class="st-green">Yes</span>' : 'No'],
                    ['Status', res.secret && res.secret.status ? badge(res.secret.status, secretStatusColor(res.secret.status)) : dash],
                  ])}
                  ${res.value ? `<div class="small muted" style="font-weight:700;margin:10px 0 4px">NEW VALUE (SHOWN ONCE)</div>
                    <div class="secret-val"><span class="sv" style="user-select:all">${esc(res.value)}</span>
                      <button class="icon-btn" id="rtCopy" title="Copy">${ICONS.copy}</button></div>`
                    : `<div class="small faint" style="margin-top:10px">${res.recorded_upstream
                        ? esc(`Recorded — the material stays in ${vault}; nothing was generated.`)
                        : 'You supplied the material, so there is nothing new to hand back.'}</div>`}`;
                const cb = out.querySelector('#rtCopy');
                if(cb) cb.addEventListener('click', ()=>copyText(res.value, 'New secret value copied'));
                toast('success','Secret rotated', `${r.name} — next rotation ${res.next_rotation_at ? relTime(ts(res.next_rotation_at)) : 'not scheduled'}.`);
                refreshAll();
                if(res.secret) showSecret(res.secret);
              } catch (err) {
                fail(out, err, null, 'the rotation');
                toast('error','Rotation failed', msgOf(err));
              }
            }},
          ],
        });
      }

      function disable(r){
        if(!allowed('admin','Disabling a secret')) return;
        openModal({
          title:'Disable Secret', icon:'xCircle',
          body:`<p style="margin-top:0">Disable <b style="color:var(--text)">${esc(r.name)}</b>?</p>
            <p class="small">Linked systems lose access immediately. The credential and its history are kept.</p>
            <div class="form-row"><label>REASON</label><input class="input" id="dsReason" placeholder="Recorded in the access log"></div>`,
          footer:[
            {label:'Cancel'},
            {label:'Disable', cls:'danger', onClick: async (close, modal) => {
              try {
                const saved = await Store.mutate(() => API.secrets.disable(r.id, {
                  reason: (modal.querySelector('#dsReason').value || '').trim() || null,
                }), { event:'secrets:changed' });
                close();
                toast('warn','Secret disabled', `${saved.name} is out of service.`);
                refreshAll();
                showSecret(saved);
              } catch (err) {
                toast('error','Could not disable', msgOf(err));
              }
            }},
          ],
        });
      }

      async function enable(r){
        if(!allowed('admin','Enabling a secret')) return;
        try {
          const saved = await Store.mutate(() => API.secrets.enable(r.id), { event:'secrets:changed' });
          toast('success','Secret enabled', `${saved.name} is back in service.`);
          refreshAll();
          showSecret(saved);
        } catch (err) {
          toast('error','Could not enable', msgOf(err));
        }
      }

      function removeSecret(r){
        if(!allowed('admin','Deleting a secret')) return;
        confirmModal({
          title:'Delete Secret', danger:true, confirmLabel:'Delete',
          body:`<p style="margin-top:0">Delete <b style="color:var(--text)">${esc(r.name)}</b> and its access history?</p>
            <p class="small">This cannot be undone. Disable the secret instead if you may need the history.</p>`,
          onConfirm: async () => {
            try {
              await Store.mutate(() => API.secrets.remove(r.id), { event:'secrets:changed' });
              toast('success','Secret deleted', `${r.name} removed from the vault index.`);
              current = null;
              document.getElementById('scLayout').classList.add('collapsed');
              refreshAll();
            } catch (err) {
              toast('error','Could not delete', msgOf(err));
            }
          },
        });
      }

      function auditLog(r){
        openModal({
          title:'Audit Log — ' + r.name, icon:'history', wide:true,
          body:'<div id="salHost"></div>',
          footer:[{label:'Close'}],
          onOpen(modal){
            const host = modal.querySelector('#salHost');
            const t = dataTable({
              rowId:'id', pageSize:10, itemName:'access events',
              searchPlaceholder:'Search the access log…',
              defaultSort:{ key:'occurred_at', dir:-1 },
              emptyText:'Nothing has touched this secret yet',
              columns:[
                {key:'occurred_at', label:'Time', render:r2=>`<span class="dim nowrap">${when(r2.occurred_at)}</span>`},
                {key:'actor', label:'Actor', render:r2=>text(r2.actor)},
                {key:'action', label:'Event', render:r2=>`<b>${esc(r2.action || '')}</b>`},
                {key:'success', label:'Result', render:r2=>r2.success?statusText('Allowed','green'):statusText('Denied','red')},
                {key:'ip_address', label:'IP', sortable:false, render:r2=>dim(r2.ip_address)},
                {key:'justification', label:'Justification', sortable:false, render:r2=>dim(clip(r2.justification || '', 60))},
              ],
              filters:[
                {key:'action', label:'Event', param:'action', options:['reveal','rotate','use','disable','enable','create','update'], allLabel:'All Events'},
              ],
              source: (params) => API.secrets.accessLog(r.id, params),
            });
            host.appendChild(t.filterEl);
            host.appendChild(t.el);
          },
        });
      }

      function copyText(value, okMsg){
        if(!value){ toast('error','Nothing to copy','The server did not return a value.'); return; }
        if(navigator.clipboard && navigator.clipboard.writeText){
          navigator.clipboard.writeText(value)
            .then(()=>toast('success', okMsg, null))
            .catch(()=>toast('error','Could not copy','The browser refused clipboard access.'));
        } else {
          toast('error','Could not copy','This browser does not expose the clipboard.');
        }
      }

      /* ------------------------------ inspector ------------------------------ */

      function showSecret(row){
        if(!row) return;
        current = row;
        const layout = document.getElementById('scLayout');
        const insp = document.getElementById('scInspector');
        if(!layout || !insp) return;
        layout.classList.remove('collapsed');
        insp.innerHTML = `<div class="insp-head"><div class="grow"><div class="insp-title">${esc(row.name)}</div>
            <div class="insp-sub">Loading…</div></div>
            <button class="icon-btn insp-close" id="scClose">${ICONS.x}</button></div>${LOADING(220)}`;
        insp.querySelector('#scClose').addEventListener('click', ()=>layout.classList.add('collapsed'));
        API.secrets.get(row.id)
          .then(s => { if(current && current.id === row.id) paintSecret(insp, s); })
          .catch(err => {
            if(!current || current.id !== row.id) return;
            const holder = insp.querySelector('.card-loading');
            if(holder) holder.replaceWith(screenError(err, ()=>showSecret(row), 'this secret'));
          });
      }

      function paintSecret(insp, s){
        current = s;
        insp.innerHTML = `
          <div class="insp-head">
            <span class="entity-ico" style="width:38px;height:38px;background:var(--green-dim);color:#15803D">${ICONS.key}</span>
            <div class="grow"><div class="insp-title">${esc(s.name)}</div>
              <div class="flex" style="gap:6px;margin-top:4px">${s.status?badge(s.status, secretStatusColor(s.status)):''}${s.risk?riskBadge(s.risk):''}</div></div>
            <button class="icon-btn insp-close" id="scClose">${ICONS.x}</button></div>
          ${inspSection('Overview','info', kv([
            ['Type', text(s.secret_type)],
            ['Vault', text(s.vault)],
            ['Environment', s.environment?badge(s.environment):dash],
            ['Owner', s.owner_name ? esc(s.owner_name) + (s.owner_email ? ` (${esc(s.owner_email)})` : '') : dash],
            ['Created On', day(s.created_at)],
            ['Created By', text(s.created_by)],
            ['Last Updated', when(s.updated_at)],
            ['Privileged', s.privileged ? '<span class="st-amber">Yes</span>' : 'No'],
          ]) + `<div class="small muted" style="font-weight:700;margin:9px 0 4px">SECRET VALUE</div>
          <div class="secret-val"><span class="sv" id="svVal">${s.display_hint ? esc(s.display_hint) : (s.has_material ? '••••••••••••' : 'No material stored')}</span>
            <button class="icon-btn" id="svEye" title="Reveal (audited)">${ICONS.eye}</button>
            <button class="icon-btn" id="svCopy" title="Copy vault reference">${ICONS.copy}</button></div>
          <div class="small faint" style="margin-top:5px">${s.vault_reference ? esc(s.vault_reference) : 'No vault reference recorded.'}</div>
          <div class="small faint" style="margin-top:3px">Values live in the vault. Reveals are audited and shown once.</div>`)}
          ${inspSection('Rotation & Compliance','refresh', kv([
            ['Rotation Policy', s.rotation_period_days == null ? dash : `Every ${s.rotation_period_days} days`],
            ['Last Rotated', when(s.last_rotated_at)],
            ['Next Rotation', s.is_rotation_overdue ? '<span class="st-red">Overdue</span>'
              : s.next_rotation_at ? `${s.is_expiring_soon?'<span class="st-amber">':''}${when(s.next_rotation_at)}${s.is_expiring_soon?'</span>':''}` : dash],
            ['Rotation Label', text(s.rotation_label)],
            ['Expires', when(s.expires_at)],
            ['Compliance', s.compliance ? badge(s.compliance, s.compliance === 'Compliant' ? 'green' : s.compliance === 'N/A' ? 'gray' : 'red') : dash],
            ['Material Stored', s.has_material ? '<span class="st-green">Yes</span>' : '<span class="faint">No — reference only</span>'],
          ]))}
          ${inspSection('Access & Usage','users', kv([
            ['Last Accessed', when(s.last_accessed_at)],
            ['Risk', s.risk?riskBadge(s.risk):dash],
            ['Updated By', text(s.updated_by)],
          ]))}
          <div id="scRecent">${inspSection('Recent Access','history', LOADING(110))}</div>
          <div class="insp-section"><div class="insp-section-title">Quick Actions</div>
            <div class="grid g2" style="gap:8px">
              <button class="btn sm primary" id="qaRotate">${ICONS.refresh}Rotate Secret</button>
              <button class="btn sm" id="qaAudit">${ICONS.history}View Audit Log</button>
              <button class="btn sm" id="qaAccess">${ICONS.lock}Edit Access</button>
              ${s.status === 'Disabled'
                ? `<button class="btn sm success" id="qaEnable">${ICONS.checkCircle}Enable Secret</button>`
                : s.status === 'Revoked'
                  ? `<button class="btn sm" disabled title="A revoked credential cannot be enabled or disabled again.">${ICONS.xCircle}Revoked — final</button>`
                  : `<button class="btn sm ghost-danger" id="qaDisable">${ICONS.xCircle}Disable Secret</button>`}
            </div></div>`;
        insp.querySelector('#scClose').addEventListener('click', ()=>document.getElementById('scLayout').classList.add('collapsed'));

        const eye = requireRole(insp.querySelector('#svEye'), 'admin', 'Revealing a secret');
        if(!s.has_material && !eye.disabled){
          eye.disabled = true;
          eye.title = 'No material is stored for this credential — only a vault reference.';
        }
        eye.addEventListener('click', ()=>reveal(s));
        insp.querySelector('#svCopy').addEventListener('click', ()=>
          copyText(s.vault_reference, 'Vault reference copied'));
        const rot = requireRole(insp.querySelector('#qaRotate'), 'admin', 'Rotating a secret');
        if((s.status === 'Disabled' || s.status === 'Revoked') && !rot.disabled){
          // The server answers 412: a credential out of service is not rotated.
          rot.disabled = true;
          rot.title = s.status === 'Disabled' ? 'Enable this secret before rotating it.'
            : 'A revoked credential cannot be rotated.';
        }
        rot.addEventListener('click', ()=>rotate(s));
        insp.querySelector('#qaAudit').addEventListener('click', ()=>auditLog(s));
        requireRole(insp.querySelector('#qaAccess'), 'admin', 'Editing secret access')
          .addEventListener('click', ()=>editAccess(s));
        const dis = insp.querySelector('#qaDisable');
        if(dis) requireRole(dis, 'admin', 'Disabling a secret').addEventListener('click', ()=>disable(s));
        const en = insp.querySelector('#qaEnable');
        if(en) requireRole(en, 'admin', 'Enabling a secret').addEventListener('click', ()=>enable(s));

        loadRecentAccess(s);
      }

      function loadRecentAccess(s){
        const host = document.getElementById('scRecent');
        if(!host) return;
        API.secrets.accessLog(s.id, { page_size: 6, sort:'-occurred_at' })
          .then(page => {
            if(!document.getElementById('scRecent')) return;
            const items = page.items || [];
            host.innerHTML = inspSection('Recent Access','history', items.length
              ? kv(items.map(e=>[`${e.action} · ${e.actor || 'system'}`,
                  `<span class="small ${e.success?'st-green':'st-red'}">${e.success?'Allowed':'Denied'}</span> <span class="faint small">${rel(e.occurred_at)}</span>`]))
                + `<div class="small faint" style="margin-top:6px">${fmtFull(page.total || 0)} events recorded</div>`
              : EMPTY('history','No access recorded','Nothing has read or changed this credential yet.'));
          })
          .catch(err => {
            if(!document.getElementById('scRecent')) return;
            host.innerHTML = '';
            host.appendChild(screenError(err, ()=>loadRecentAccess(s), 'the access history'));
          });
      }
    },
  };
})();
