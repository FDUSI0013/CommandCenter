/* Fulcrum Ops — shared UI components */
(function(){
  'use strict';
  const { esc, fmtFull, initials, avColor } = U;

  function elem(html){
    const t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }

  // ---------------- badges ----------------
  const BADGE_COLOR = {
    // status
    'Active':'green','Connected':'green','Healthy':'green','Completed':'green','Success':'green','Successful':'green',
    'Allowed':'green','Passed':'green','Compliant':'green','Deployed':'green','Approved':'green','Enabled':'green',
    'Up to date':'green','Good':'green','Met':'green','Resolved':'green','Live':'green','Acknowledged':'green','Synced':'green',
    // amber
    'Warning':'amber','Warned':'amber','Degraded':'amber','Expiring Soon':'amber','Due Soon':'amber','Draft':'amber',
    'Pending':'amber','Syncing':'amber','Neutral':'amber','Medium':'amber','In Review':'amber','Running':'amber',
    'Scheduled':'amber','Investigating':'amber','Stale':'amber','Throttled':'amber','Paused':'amber','In Progress':'amber',
    // red
    'Blocked':'red','Failed':'red','Disconnected':'red','Expired':'red','High':'red','Critical':'red','Overdue':'red',
    'Rejected':'red','Negative':'red','Error':'red','Unhealthy':'red','Suspended':'red','Revoked':'red','Violation':'red',
    'Non-Compliant':'red','Rollback':'red','Rolled Back':'red','Escalated':'red','Open':'red','Disabled':'gray',
    // others
    'Low':'green','Inactive':'gray','Archived':'gray','Deprecated':'gray','Standby':'gray','Positive':'green',
    'Review':'purple','Pending Review':'purple','Approval Required':'purple','Configuration':'purple',
    'Production':'green','Staging':'cyan','UAT':'blue','Development':'purple','QA':'amber','Sandbox':'blue','DR':'red',
    'Internal':'blue','External':'orange','Public':'amber','Confidential':'orange','Highly Confidential':'red','Restricted':'red',
  };
  function badge(text, color, dot){
    const c = color || BADGE_COLOR[text] || 'gray';
    return `<span class="badge bg-${c}">${dot?'<span class="bdot"></span>':''}${esc(text)}</span>`;
  }
  function statusText(text, color){
    const c = color || BADGE_COLOR[text] || 'gray';
    const map = {green:'st-green',red:'st-red',amber:'st-amber',gray:'st-gray',blue:'st-blue',purple:'st-purple',cyan:'st-blue',orange:'st-amber'};
    return `<span class="status-text ${map[c]||'st-gray'}"><span class="dot" style="background:currentColor"></span>${esc(text)}</span>`;
  }
  function riskBadge(level){ return badge(level, level==='High'||level==='Critical'?'red':level==='Medium'?'amber':'green'); }
  function avatarHtml(name, sm){
    return `<span class="avatar ${sm?'sm':''} ${avColor(name)}" title="${esc(name)}">${initials(name)}</span>`;
  }
  function ownerCell(name, team){
    return `<div class="owner-cell">${avatarHtml(name, true)}<div><div class="cell-main" style="font-size:12px">${esc(name)}</div>${team?`<div class="cell-sub">${esc(team)}</div>`:''}</div></div>`;
  }
  function entityCell(name, sub, iconName, color){
    const colMap = {purple:'var(--purple-dim);color:var(--purple-bright)',green:'var(--green-dim);color:#15803D',blue:'var(--blue-dim);color:#1D4ED8',amber:'var(--amber-dim);color:#B45309',red:'var(--red-dim);color:#B91C1C',cyan:'var(--cyan-dim);color:#0E7490',orange:'var(--orange-dim);color:#C2410C',pink:'var(--pink-dim);color:#BE185D',gray:'rgba(138,144,163,.13);color:#64748B'};
    const ic = ICONS[iconName] ? `<span class="entity-ico" style="background:${colMap[color||'purple']}">${ICONS[iconName]}</span>` : (LOGOS[iconName] ? `<span class="entity-ico" style="background:var(--panel-3)">${LOGOS[iconName]}</span>` : '');
    return `<div class="entity-cell">${ic}<div style="min-width:0"><div class="cell-main">${esc(name)}</div>${sub?`<div class="cell-sub">${esc(sub)}</div>`:''}</div></div>`;
  }
  function platformCell(platform){
    const map = { 'Azure AI Foundry':'foundry', 'Copilot Studio':'copilot', 'M365 Copilot':'m365', 'Microsoft 365':'m365', 'Custom Agent':'custom', 'Power Platform':'power' };
    const lg = LOGOS[map[platform]] || LOGOS.custom;
    return `<span class="flex" style="gap:7px"><span style="width:15px;height:15px;display:inline-flex;flex-shrink:0">${lg}</span><span class="dim" style="font-size:12px">${esc(platform)}</span></span>`;
  }

  // ---------------- KPI cards ----------------
  const ICO_BG = {purple:'background:var(--purple-dim);color:var(--purple-bright)',green:'background:var(--green-dim);color:#15803D',red:'background:var(--red-dim);color:#B91C1C',amber:'background:var(--amber-dim);color:#B45309',orange:'background:var(--orange-dim);color:#C2410C',blue:'background:var(--blue-dim);color:#1D4ED8',cyan:'background:var(--cyan-dim);color:#0E7490',gray:'background:rgba(138,144,163,.13);color:#64748B',pink:'background:var(--pink-dim);color:#BE185D'};
  function kpiCard(c){
    // {label, value, delta, dir:'up'|'down', good:bool, vs, icon, color, sub}
    let deltaHtml = '';
    if(c.delta != null){
      const cls = c.good == null ? '' : (c.good ? 'good' : 'bad');
      const arrow = c.dir === 'down' ? ICONS.arrowDown : ICONS.arrowUp;
      deltaHtml = `<div class="kpi-delta ${cls}">${c.dir?arrow:''}<span>${esc(c.delta)}</span>${c.vs?`<span class="vs">${esc(c.vs)}</span>`:''}</div>`;
    } else if(c.sub){
      deltaHtml = `<div class="kpi-sub">${esc(c.sub)}</div>`;
    }
    return `<div class="kpi-card">
      <div class="kpi-top"><div class="kpi-label">${esc(c.label)}</div>
      ${c.icon?`<div class="kpi-ico" style="${ICO_BG[c.color||'purple']}">${ICONS[c.icon]||''}</div>`:''}</div>
      <div class="kpi-value">${c.value}</div>
      ${deltaHtml}
    </div>`;
  }
  function kpiRow(cards, cols){
    return `<div class="kpi-row" ${cols?`style="grid-template-columns:repeat(auto-fit,minmax(${cols}px,1fr))"`:''}>${cards.map(kpiCard).join('')}</div>`;
  }
  function miniKpi(m){
    return `<div class="mini-kpi"><div><div class="mk-label">${esc(m.label)}</div><div class="mk-value">${m.value}</div></div>${m.spark?U.sparkline(m.spark, m.color||'green', 92, 30):''}</div>`;
  }

  // ---------------- toast ----------------
  const TOAST_ICONS = {success:'checkCircle', error:'xCircle', warn:'alert', info:'info'};
  const TOAST_COLORS = {success:'#15803D', error:'#B91C1C', warn:'#B45309', info:'#5B36D6'};
  function toast(type, title, msg, ms){
    const root = document.getElementById('toast-root');
    const el = elem(`<div class="toast ${type}">
      <span class="t-ico" style="color:${TOAST_COLORS[type]||TOAST_COLORS.info}">${ICONS[TOAST_ICONS[type]||'info']}</span>
      <div><div class="t-title">${esc(title)}</div>${msg?`<div class="t-msg">${esc(msg)}</div>`:''}</div>
    </div>`);
    root.appendChild(el);
    setTimeout(()=>{ el.classList.add('out'); setTimeout(()=>el.remove(), 280); }, ms||3800);
  }

  // ---------------- modal ----------------
  function openModal(cfg){
    const root = document.getElementById('modal-root');
    const overlay = elem(`<div class="modal-overlay"></div>`);
    const foot = (cfg.footer||[]).map((b,i)=>`<button class="btn ${b.cls||''}" data-mbtn="${i}">${b.icon?ICONS[b.icon]:''}${esc(b.label)}</button>`).join('');
    const modal = elem(`<div class="modal ${cfg.wide?'wide':''}">
      <div class="modal-head">${cfg.icon?`<span style="color:var(--purple-bright);display:inline-flex;width:17px">${ICONS[cfg.icon]}</span>`:''}<div class="modal-title">${esc(cfg.title)}</div>
        <button class="icon-btn" style="margin-left:auto" data-mclose>${ICONS.x}</button></div>
      <div class="modal-body">${cfg.body||''}</div>
      ${foot?`<div class="modal-foot">${foot}</div>`:''}
    </div>`);
    overlay.appendChild(modal);
    root.appendChild(overlay);
    const close = ()=> overlay.remove();
    overlay.addEventListener('click', e=>{ if(e.target===overlay) close(); });
    modal.querySelector('[data-mclose]').addEventListener('click', close);
    (cfg.footer||[]).forEach((b,i)=>{
      modal.querySelector(`[data-mbtn="${i}"]`).addEventListener('click', ()=>{
        if(b.onClick) b.onClick(close, modal);
        if(b.close !== false && !b.onClick) close();
      });
    });
    if(cfg.onOpen) cfg.onOpen(modal, close);
    return { close, el: modal };
  }
  function confirmModal(cfg){
    openModal({
      title: cfg.title, icon: cfg.icon || 'alert',
      body: cfg.body || `<p style="margin:0">${esc(cfg.msg||'Are you sure?')}</p>`,
      footer: [
        {label:'Cancel', onClick:(close)=>close()},
        {label:cfg.confirmLabel||'Confirm', cls:cfg.danger?'danger':'primary', onClick:(close, modal)=>{ close(); cfg.onConfirm && cfg.onConfirm(modal); }}
      ]
    });
  }

  // ---------------- dropdown menu ----------------
  let activeMenu = null;
  function closeMenu(){ if(activeMenu){ activeMenu.remove(); activeMenu = null; } }
  document.addEventListener('click', e=>{ if(activeMenu && !activeMenu.contains(e.target)) closeMenu(); }, true);
  window.addEventListener('resize', closeMenu);
  function openMenu(anchor, items){
    closeMenu();
    const root = document.getElementById('dropdown-root');
    const menu = elem(`<div class="menu">${items.map((it,i)=>{
      if(it.sep) return '<div class="menu-sep"></div>';
      if(it.head) return `<div class="menu-head">${esc(it.head)}</div>`;
      return `<div class="menu-item ${it.danger?'danger':''}" data-mi="${i}">${it.icon?ICONS[it.icon]:''}${esc(it.label)}</div>`;
    }).join('')}</div>`);
    root.appendChild(menu);
    const r = anchor.getBoundingClientRect();
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    let x = Math.min(r.right - mw, window.innerWidth - mw - 8);
    if(x < 8) x = Math.min(r.left, window.innerWidth - mw - 8);
    let y = r.bottom + 5;
    if(y + mh > window.innerHeight - 8) y = r.top - mh - 5;
    menu.style.left = Math.max(8,x) + 'px';
    menu.style.top = Math.max(8,y) + 'px';
    items.forEach((it,i)=>{
      if(it.sep || it.head) return;
      const el = menu.querySelector(`[data-mi="${i}"]`);
      el.addEventListener('click', (e)=>{ e.stopPropagation(); closeMenu(); it.onClick && it.onClick(); });
    });
    activeMenu = menu;
    // keep menu open across the initiating click
    setTimeout(()=>{},0);
  }

  // ---------------- page head ----------------
  function pageHead(cfg){
    // {title, sub, crumbs, actions: html string}
    return `${cfg.crumbs?`<div class="crumbs">${cfg.crumbs}</div>`:''}
    <div class="page-head">
      <div><h1 class="page-title">${esc(cfg.title)}</h1>${cfg.sub?`<div class="page-sub">${esc(cfg.sub)}</div>`:''}</div>
      <div class="page-actions">${cfg.actions||''}</div>
    </div>`;
  }
  function searchBox(id, placeholder, width){
    return `<div class="search-box" ${width?`style="min-width:${width}px"`:''}>${ICONS.search}<input id="${id}" type="text" placeholder="${esc(placeholder)}"></div>`;
  }

  // ---------------- tabs ----------------
  function tabBar(container, tabs, onChange, activeIdx){
    // tabs: [{label, count}]
    const el = elem(`<div class="tabs">${tabs.map((t,i)=>`<div class="tab ${i===(activeIdx||0)?'active':''}" data-tab="${i}">${esc(t.label)}${t.count!=null?`<span class="tab-count">${t.count}</span>`:''}</div>`).join('')}</div>`);
    el.addEventListener('click', e=>{
      const tb = e.target.closest('.tab'); if(!tb) return;
      el.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
      tb.classList.add('active');
      onChange(parseInt(tb.dataset.tab,10), tb);
    });
    if(typeof container === 'string') container = document.getElementById(container);
    if(container) container.appendChild(el);
    return el;
  }

  // ---------------- inspector helpers ----------------
  function inspSection(title, iconName, bodyHtml){
    return `<div class="insp-section"><div class="insp-section-title">${iconName?ICONS[iconName]:''}${esc(title)}</div>${bodyHtml}</div>`;
  }
  function kv(rows){
    return rows.filter(r=>r).map(r=>`<div class="kv"><span class="k">${esc(r[0])}</span><span class="v ${r[2]||''}">${r[1]}</span></div>`).join('');
  }

  // ---------------- data table ----------------
  let tblUid = 0;
  function dataTable(cfg){
    /* cfg: {
        columns: [{key,label,render,csv,sortVal,sortable,align,width}],
        rows, pageSize, pageSizes, searchKeys, rowId,
        filters: [{key,label,options,getVal,param}],   // select filters
        onSelect(row, tr), rowActions(row)->menu items, exportName,
        selectable (checkbox col), defaultSort:{key,dir}, emptyText, autoSelectFirst,

        // --- server mode ---------------------------------------------------
        source(params) -> Promise<{items,total,page,page_size,pages}>
            When present the table stops filtering in the browser: search, sort,
            filters and paging become query parameters and every change re-asks
            the server. `params` is {page, page_size, q, sort, ...filters}, where
            each filter contributes `filter.param || filter.key`.
        exportSource(params) -> Promise   server-side CSV for the filtered set.
    } */
    const id = 'tbl' + (++tblUid);
    const server = typeof cfg.source === 'function';
    const state = {
      query:'', filters:{}, sortKey: cfg.defaultSort?cfg.defaultSort.key:null,
      sortDir: cfg.defaultSort?cfg.defaultSort.dir:1, page:1,
      pageSize: cfg.pageSize||10, selectedId:null,
      rows: (cfg.rows||[]).slice(),
      total: (cfg.rows||[]).length, loading: false, error: null, seq: 0,
    };

    /** The query the server needs to answer the question the UI is asking. */
    function params(){
      const out = { page: state.page, page_size: state.pageSize };
      if(state.query) out.q = state.query;
      if(state.sortKey) out.sort = (state.sortDir < 0 ? '-' : '') + state.sortKey;
      (cfg.filters||[]).forEach(f=>{
        const v = state.filters[f.key];
        if(v) out[f.param || f.key] = v;
      });
      return Object.assign(out, cfg.extraParams || {});
    }

    /* One in-flight request at a time wins: a slow page-1 response must never
       overwrite the page-2 rows the user is already looking at. */
    async function load(){
      if(!server) { render(); return; }
      const ticket = ++state.seq;
      state.loading = true; state.error = null;
      render();
      try {
        const page = await cfg.source(params());
        if(ticket !== state.seq) return;
        state.rows = (page && page.items) || [];
        state.total = page && page.total != null ? page.total : state.rows.length;
        state.loading = false;
        render();
        if(cfg.onLoad) cfg.onLoad(state.rows, page);
      } catch (err) {
        if(ticket !== state.seq) return;
        state.loading = false;
        state.error = err;
        state.rows = [];
        render();
      }
    }

    let searchTimer = null;
    function changed(resetPage){
      if(resetPage !== false) state.page = 1;
      if(server) load(); else render();
    }
    const wrap = elem(`<div class="card pad-0"><div class="tbl-wrap" style="max-height:${cfg.maxHeight||'none'}"><table class="tbl"><thead></thead><tbody></tbody></table></div><div class="tbl-foot"></div></div>`);
    const thead = wrap.querySelector('thead'), tbody = wrap.querySelector('tbody'), foot = wrap.querySelector('.tbl-foot');

    let filterBar = null;
    if(cfg.filters && cfg.filters.length){
      filterBar = elem(`<div class="filter-bar">${cfg.searchable!==false?`<div class="filter-item"><label>Search</label><div class="filter-search">${ICONS.search}<input type="text" placeholder="${esc(cfg.searchPlaceholder||'Search…')}" data-tsearch></div></div>`:''}
        ${cfg.filters.map((f,i)=>`<div class="filter-item"><label>${esc(f.label)}</label><select class="filter-select" data-fi="${i}"><option value="">${esc(f.allLabel||'All')}</option>${f.options.map(o=>`<option>${esc(o)}</option>`).join('')}</select></div>`).join('')}
        <span class="clear-filters">Clear All</span></div>`);
      filterBar.querySelectorAll('[data-fi]').forEach(sel=>{
        sel.addEventListener('change', ()=>{
          state.filters[cfg.filters[sel.dataset.fi].key] = sel.value;
          changed();
        });
      });
      const si = filterBar.querySelector('[data-tsearch]');
      if(si) si.addEventListener('input', ()=>{
        state.query = si.value;
        // Typing must not fire a request per keystroke.
        if(server){ clearTimeout(searchTimer); searchTimer = setTimeout(changed, 250); }
        else changed();
      });
      filterBar.querySelector('.clear-filters').addEventListener('click', ()=>{
        state.filters = {}; state.query = '';
        filterBar.querySelectorAll('select').forEach(s=>s.value='');
        const si2 = filterBar.querySelector('[data-tsearch]'); if(si2) si2.value='';
        changed();
      });
    }

    function getFiltered(){
      // In server mode the rows in hand are already the answer to the query.
      if(server) return state.rows;
      let rows = state.rows;
      if(state.query){
        const q = state.query.toLowerCase();
        const keys = cfg.searchKeys || cfg.columns.map(c=>c.key);
        rows = rows.filter(r => keys.some(k=>{
          const v = typeof k === 'function' ? k(r) : r[k];
          return v != null && String(v).toLowerCase().includes(q);
        }));
      }
      if(cfg.filters){
        cfg.filters.forEach(f=>{
          const val = state.filters[f.key];
          if(val){
            if(f.match) rows = rows.filter(r => f.match(r, val));
            else rows = rows.filter(r => String(f.getVal ? f.getVal(r) : r[f.key]) === val);
          }
        });
      }
      if(state.sortKey){
        const col = cfg.columns.find(c=>c.key===state.sortKey);
        const gv = col && col.sortVal ? col.sortVal : (r=>r[state.sortKey]);
        rows = rows.slice().sort((a,b)=>{
          const va = gv(a), vb = gv(b);
          if(va == null) return 1; if(vb == null) return -1;
          if(typeof va === 'number' && typeof vb === 'number') return (va-vb)*state.sortDir;
          return String(va).localeCompare(String(vb))*state.sortDir;
        });
      }
      return rows;
    }

    function renderHead(){
      thead.innerHTML = `<tr>${cfg.selectable?`<th style="width:34px"><input type="checkbox" data-selall></th>`:''}${cfg.columns.map(c=>{
        const sortable = c.sortable !== false;
        const ind = state.sortKey === c.key ? `<span class="sort-ico">${state.sortDir>0?'▲':'▼'}</span>` : '';
        return `<th class="${sortable?'sortable':''}" data-col="${c.key}" style="${c.align?`text-align:${c.align};`:''}${c.width?`width:${c.width};`:''}">${esc(c.label)}${ind}</th>`;
      }).join('')}${cfg.rowActions?'<th style="width:40px"></th>':''}</tr>`;
      thead.querySelectorAll('th.sortable').forEach(th=>{
        th.addEventListener('click', ()=>{
          const k = th.dataset.col;
          if(state.sortKey === k) state.sortDir *= -1; else { state.sortKey = k; state.sortDir = 1; }
          changed();
        });
      });
    }

    function render(){
      renderHead();
      const rows = getFiltered();
      const total = server ? state.total : rows.length;
      const pages = Math.max(1, Math.ceil(total/state.pageSize));
      if(state.page > pages) state.page = pages;
      const start = (state.page-1)*state.pageSize;
      // The server already sliced; the browser slices only what it holds.
      const pageRows = server ? rows : rows.slice(start, start + state.pageSize);
      const span = cfg.columns.length + 2;

      if(state.loading && !pageRows.length){
        tbody.innerHTML = `<tr><td colspan="${span}"><div class="tbl-loading">${
          Array.from({length: Math.min(state.pageSize, 6)}, ()=>`<div class="skeleton-row"></div>`).join('')
        }</div></td></tr>`;
      } else if(state.error){
        const msg = state.error && state.error.message ? state.error.message : 'The request failed.';
        tbody.innerHTML = `<tr><td colspan="${span}"><div class="empty-state">${ICONS.alert}
          <div class="es-title">Could not load ${esc(cfg.itemName||'records')}</div>
          <div>${esc(msg)}</div>
          <button class="btn" data-retry style="margin-top:12px">Try again</button></div></td></tr>`;
        const rb = tbody.querySelector('[data-retry]');
        if(rb) rb.addEventListener('click', load);
      } else if(!pageRows.length){
        tbody.innerHTML = `<tr><td colspan="${span}"><div class="empty-state">${ICONS.search}<div class="es-title">${esc(cfg.emptyText||'No records match your filters')}</div><div>Try adjusting the search or filter criteria.</div></div></td></tr>`;
      } else {
        tbody.innerHTML = pageRows.map(r=>{
          const rid = cfg.rowId ? r[cfg.rowId] : rows.indexOf(r);
          return `<tr data-rid="${esc(rid)}" class="${state.selectedId===rid?'selected':''} ${r.__flash?'flash-in':''}">
            ${cfg.selectable?`<td><input type="checkbox" data-rowsel onclick="event.stopPropagation()"></td>`:''}
            ${cfg.columns.map(c=>`<td class="${c.cls||''}" style="${c.align?`text-align:${c.align};`:''}">${c.render?c.render(r):esc(r[c.key])}</td>`).join('')}
            ${cfg.rowActions?`<td class="right"><button class="icon-btn" data-ract>${ICONS.dots}</button></td>`:''}
          </tr>`;
        }).join('');
        pageRows.forEach(r=>{ delete r.__flash; });
      }

      // footer
      const fromN = total ? start+1 : 0, toN = Math.min(start+state.pageSize, total);
      let pageBtns = '';
      const addBtn = (p, label, cur) => { pageBtns += `<button class="page-btn ${cur?'cur':''}" data-pg="${p}">${label}</button>`; };
      pageBtns += `<button class="page-btn" data-pg="prev" ${state.page===1?'disabled':''}>${ICONS.chevLeft}</button>`;
      const windowPages = [];
      for(let p=1;p<=pages;p++){
        if(p===1 || p===pages || Math.abs(p-state.page)<=1) windowPages.push(p);
      }
      let last = 0;
      windowPages.forEach(p=>{
        if(p-last>1) pageBtns += `<span class="faint" style="padding:0 3px">…</span>`;
        addBtn(p, p, p===state.page); last = p;
      });
      pageBtns += `<button class="page-btn" data-pg="next" ${state.page===pages?'disabled':''}>${ICONS.chevRight}</button>`;
      foot.innerHTML = `<div>Showing <b style="color:var(--text)">${fromN}–${toN}</b> of <b style="color:var(--text)">${fmtFull(cfg.totalOverride||total)}</b> ${esc(cfg.itemName||'records')}</div>
        <div class="flex" style="gap:16px">
          <div class="rows-select">Rows per page <select class="filter-select" style="min-width:58px;height:26px" data-ps>${(cfg.pageSizes||[10,25,50]).map(n=>`<option ${n===state.pageSize?'selected':''}>${n}</option>`).join('')}</select></div>
          <div class="pager">${pageBtns}</div>
        </div>`;
      foot.querySelectorAll('[data-pg]').forEach(b=>{
        b.addEventListener('click', ()=>{
          const v = b.dataset.pg;
          if(v==='prev') state.page = Math.max(1, state.page-1);
          else if(v==='next') state.page = Math.min(pages, state.page+1);
          else state.page = parseInt(v,10);
          changed(false);
        });
      });
      foot.querySelector('[data-ps]').addEventListener('change', e=>{ state.pageSize = parseInt(e.target.value,10); changed(); });

      // row events
      tbody.querySelectorAll('tr[data-rid]').forEach(tr=>{
        tr.addEventListener('click', (e)=>{
          if(e.target.closest('[data-ract]')) return;
          const rid = tr.dataset.rid;
          const row = state.rows.find(r=>String(cfg.rowId?r[cfg.rowId]:'') === rid) || pageRows[[...tbody.children].indexOf(tr)];
          state.selectedId = cfg.rowId ? row[cfg.rowId] : null;
          tbody.querySelectorAll('tr').forEach(x=>x.classList.remove('selected'));
          tr.classList.add('selected');
          cfg.onSelect && cfg.onSelect(row, tr);
        });
        const ab = tr.querySelector('[data-ract]');
        if(ab) ab.addEventListener('click', (e)=>{
          e.stopPropagation();
          const rid = tr.dataset.rid;
          const row = state.rows.find(r=>String(cfg.rowId?r[cfg.rowId]:'') === rid);
          openMenu(ab, cfg.rowActions(row));
        });
      });
    }

    const api = {
      el: wrap, filterEl: filterBar, state,
      /** Re-ask the server (server mode) or repaint what is held (local mode). */
      refresh(){ if(server) load(); else render(); },
      reload(){ return load(); },
      params,
      setRows(rows){ state.rows = rows.slice(); state.total = rows.length; render(); },
      getRows(){ return state.rows; },
      /**
       * Put a newly arrived row on top. Used by the run stream.
       *
       * Only while the reader is on page one with no search or filters applied:
       * anywhere else, injecting a row would contradict the query on screen, so
       * the row is left for the next fetch. The count is bumped only when no
       * filter or search is active — the arriving row may not match the query,
       * so it must not inflate a filtered total.
       */
      prependRow(row){
        const filtered = state.query || Object.values(state.filters).some(Boolean);
        if(!filtered) state.total += 1;
        if(state.page !== 1 || filtered){ render(); return false; }
        row.__flash = true;
        state.rows.unshift(row);
        if(state.rows.length > state.pageSize) state.rows.length = state.pageSize;
        render();
        return true;
      },
      search(q){ state.query = q; changed(); },
      getFiltered,
      selectFirst(){
        const rows = getFiltered();
        if(rows.length && cfg.onSelect){
          state.selectedId = cfg.rowId ? rows[0][cfg.rowId] : null;
          render();
          cfg.onSelect(rows[0], null);
        }
      },
      async export(){
        // The server exports the whole filtered set; the browser can only
        // export the rows it holds, which in server mode is one page.
        if(server && typeof cfg.exportSource === 'function'){
          try {
            await cfg.exportSource(params());
            toast('success','Export complete', `${cfg.itemName||'Records'} exported to CSV.`);
          } catch (err) {
            toast('error','Export failed', (err && err.message) || 'The export could not be generated.');
          }
          return;
        }
        U.downloadCSV(cfg.exportName||'export', cfg.columns, getFiltered());
        toast('success','Export complete', `${getFiltered().length} ${cfg.itemName||'records'} exported to CSV.`);
      }
    };

    if(server){
      load().then(()=>{ if(cfg.autoSelectFirst) api.selectFirst(); });
    } else {
      render();
      if(cfg.autoSelectFirst) api.selectFirst();
    }
    return api;
  }

  // ---------------- async states ----------------

  /**
   * The block a screen shows when a call it cannot render without has failed.
   *
   * @param {Error} err     the API error; its message is written for a person
   * @param {function} retry  re-runs the same call
   * @param {string} what   what could not be loaded, e.g. "the run history"
   */
  function screenError(err, retry, what){
    const msg = (err && err.message) || 'The request failed.';
    const el = elem(`<div class="screen-error">${ICONS.alert}
      <div class="se-title">Could not load ${esc(what || 'this screen')}</div>
      <div>${esc(msg)}</div>
      ${retry ? '<button class="btn" data-retry style="margin-top:12px">Try again</button>' : ''}
    </div>`);
    const btn = el.querySelector('[data-retry]');
    if(btn) btn.addEventListener('click', retry);
    return el;
  }

  /**
   * KPI cards before their numbers arrive: real labels, no invented values.
   * Replace the container's contents with `kpiRow(...)` once the summary lands.
   */
  function kpiSkeleton(labels, cols){
    return `<div class="kpi-row" ${cols?`style="grid-template-columns:repeat(auto-fit,minmax(${cols}px,1fr))"`:''}>${
      labels.map(l=>`<div class="kpi-card card-loading"><div class="kpi-top"><div class="kpi-label">${esc(l)}</div></div><div class="kpi-value">&nbsp;</div></div>`).join('')
    }</div>`;
  }

  window.C = { elem, badge, statusText, riskBadge, avatarHtml, ownerCell, entityCell, platformCell,
    kpiCard, kpiRow, kpiSkeleton, miniKpi, toast, openModal, confirmModal, openMenu, closeMenu,
    pageHead, searchBox, tabBar, inspSection, kv, dataTable, screenError };
})();
