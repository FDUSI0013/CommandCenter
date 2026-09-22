/* FD AI Command Center — OPERATIONS screens: Quota Cost & Capacity, Memory & State, Deployment & Environment
 *
 * Cost and capacity come from measured usage, memory from the stores and the
 * live sessions behind them, deployments from a pipeline the server actually
 * runs. Nothing on these screens is modelled in the browser: where a number is
 * absent it renders as "—", and where a call fails the screen says so.
 */
(function(){
  'use strict';
  window.SCREENS = window.SCREENS || {};
  const { esc, fmtNum, fmtFull, fmtMoney, relTime, fmtDateTime, sparkline, lineChart, donut, barPct } = U;
  const { badge, statusText, kpiRow, kpiCard, kpiSkeleton, dataTable, pageHead, searchBox, tabBar,
          inspSection, kv, toast, openModal, confirmModal, screenError, avatarHtml, entityCell } = C;

  const dash = '<span class="faint">—</span>';
  const pct = (v, d) => v == null ? dash : Number(v).toFixed(d == null ? 1 : d) + '%';
  const money = (v, d) => v == null ? dash : fmtMoney(v, d == null ? 0 : d);
  const ts = (v) => v ? new Date(v).getTime() : null;
  const when = (v) => v ? relTime(ts(v)) : dash;
  const healthColor = (h) => h === 'Critical' ? 'red' : h === 'Watch' ? 'amber' : 'green';
  /** A byte count. U.fmtBytes takes gigabytes, so handed bytes it called 250 KB "244.14 TB". */
  const byteSize = (n) => {
    const units = ['B','KB','MB','GB','TB'];
    let v = Number(n), u = 0;
    while(v >= 1024 && u < units.length - 1){ v /= 1024; u += 1; }
    return (u === 0 ? String(Math.round(v)) : v.toFixed(v < 10 ? 2 : 1).replace(/\.?0+$/, '')) + ' ' + units[u];
  };

  /** A KPI card from the server's MetricKpi, which carries its own display strings. */
  function serverKpi(k){
    if(!k) return { label:'—', value:'—' };
    return {
      label: k.label, value: k.display, icon: k.icon, color: k.color,
      delta: k.delta_display || null,
      dir: k.direction === 'flat' ? null : k.direction,
      good: k.good, vs: k.comparison,
      sub: k.delta_display ? null : k.sub,
    };
  }

  /** Render an async section: loading, then content, then error-with-retry. */
  function section(host, load, paint, what){
    if(!host) return;
    host.innerHTML = '<div class="card"><div class="card-loading" style="height:180px"></div></div>';
    load()
      .then(data => { if(host.isConnected) host.innerHTML = paint(data); })
      .catch(err => {
        if(!host.isConnected) return;
        host.innerHTML = '';
        host.appendChild(screenError(err, () => section(host, load, paint, what), what));
      });
  }

  const card = (title, inner, right) =>
    `<div class="card"><div class="card-head"><div class="card-title">${esc(title)}</div>${right||''}</div>${inner}</div>`;

  const emptyCard = (title, message) =>
    card(title, `<div class="empty-state">${ICONS.search}<div class="es-title">${esc(message)}</div></div>`);

  /**
   * A line chart that leaves a gap where there is no value.
   *
   * U.lineChart draws one unbroken polyline per series and reads a null as 0.
   * That is wrong for a series that is only partly there — the forecast, where
   * each day carries a measured value or a projected one and never both: the
   * Actual line fell to $0 for every day still to come and the Forecast line lay
   * along the axis for every day already gone, so the chart said "spend stops
   * tomorrow" beside a badge projecting the opposite. Here a series is drawn in
   * runs of consecutive values, each at its own place on the date axis; a run of
   * one day is a dot, since a one-point line draws nothing. Zero-based, same
   * frame and classes as U.lineChart.
   *
   * cfg: {series:[{color, points:[number|null], dashed, area}], xLabels, h, yFmt}
   */
  function gapLineChart(cfg){
    const w = 560, h = cfg.h || 200, padL = 46, padR = 12, padT = 12, padB = 24;
    const iw = w - padL - padR, ih = h - padT - padB;
    const has = v => v != null && isFinite(v);
    const values = cfg.series.reduce((all, s) => all.concat(s.points.filter(has)), []);
    // Math.max() of nothing is -Infinity, and an all-zero month still needs a scale.
    const max = ((values.length ? Math.max(...values) : 0) || 1) * 1.08;
    const n = Math.max(...cfg.series.map(s => s.points.length));
    const x = i => padL + (n > 1 ? i / (n - 1) : 0.5) * iw;
    const y = v => padT + ih - (v / max) * ih;
    const yFmt = cfg.yFmt || (v => fmtNum(Math.round(v)));
    let frame = '';
    for(let t = 0; t <= 4; t++){
      const v = (max / 4) * t, yy = y(v);
      frame += `<line x1="${padL}" y1="${yy}" x2="${w-padR}" y2="${yy}" stroke="#E6EAF2" stroke-width="1"/>
        <text x="${padL-7}" y="${yy+3.5}" text-anchor="end" class="axis-label">${esc(yFmt(v))}</text>`;
    }
    const labels = cfg.xLabels || [];
    const every = Math.ceil(labels.length / 7) || 1;
    labels.forEach((label, i) => {
      if(i % every === 0 || i === labels.length - 1){
        frame += `<text x="${x(i)}" y="${h-6}" text-anchor="middle" class="axis-label">${esc(label)}</text>`;
      }
    });
    const marks = cfg.series.map(s => {
      const col = U.cc(s.color);
      const runs = [];
      let run = [];
      s.points.forEach((v, i) => {
        if(has(v)){ run.push([i, v]); return; }
        if(run.length) runs.push(run);
        run = [];
      });
      if(run.length) runs.push(run);
      return runs.map(r => {
        if(r.length === 1) return `<circle cx="${x(r[0][0]).toFixed(1)}" cy="${y(r[0][1]).toFixed(1)}" r="2.6" fill="${col}"/>`;
        const pts = r.map(([i, v]) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
        const under = s.area
          ? `<polygon points="${x(r[0][0]).toFixed(1)},${y(0)} ${pts} ${x(r[r.length-1][0]).toFixed(1)},${y(0)}" fill="${col}" opacity="0.10"/>` : '';
        return `${under}<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2" ${s.dashed?'stroke-dasharray="5 4"':''} stroke-linejoin="round" stroke-linecap="round"/>`;
      }).join('');
    }).join('');
    return `<div class="chart-box"><svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">${frame}${marks}</svg></div>`;
  }

  /* ================= QUOTA, COST & CAPACITY ================= */

  /* The three dimensions something actually counts: ingest meters Tokens and
     Requests as batches land, and the cost sweep meters Cost. Concurrency and
     Storage are in the resource vocabulary — and stay in the table's filter, so
     a quota already written against one can be found and removed — but nothing
     reports them, so a new one would sit at 0% for ever looking enforced. The
     Create form offers only what is measured. */
  const METERED_RESOURCES = ['Tokens','Requests','Cost'];
  /* The unit a limit is written in. It is the noun quoted back in the quota's
     chip, its breach alert and the audit row, so it has to be the resource's own. */
  const RESOURCE_UNIT = { Tokens:'tokens', Requests:'requests', Cost:'USD' };

  SCREENS['quota'] = {
    title:'Quota, Cost & Capacity',
    render(main){
      main.innerHTML = `
        ${pageHead({title:'Quota, Cost & Capacity', sub:'Spend, usage quotas and infrastructure capacity across environments, services and teams.',
          actions:`${searchBox('qcSearch','Search quotas and budgets…')}
          <button class="btn" id="qcExport">${ICONS.download}Export</button>
          <button class="btn primary" id="qcCreate">${ICONS.plus}Create Quota</button>`})}
        <div id="qcKpis">${kpiSkeleton(['Total Spend (MTD)','Total Budget (MTD)','Total Tokens (MTD)','API Calls (MTD)','Avg. Cost / 1K Tokens','Capacity Health'])}</div>
        <div id="qcTabs"></div>
        <div id="qcBody"></div>`;

      const body = document.getElementById('qcBody');
      let searchTerm = '';

      /* The KPI row, the Overview cards and the Costs tab are views of one
         measurement of the period. Asked for separately they were seven reads of
         the telemetry store per visit, each measuring the same month again. The
         answer is held so a tab switch re-reads nothing; a failed read is let go
         of so "Try again" really asks again; and loadKpis — which every write on
         this screen already calls — asks afresh. */
      let overviewAsk = null;
      function overview(fresh){
        if(fresh) overviewAsk = null;
        if(!overviewAsk){
          const ask = API.quota.overview();
          overviewAsk = ask;
          ask.catch(() => { if(overviewAsk === ask) overviewAsk = null; });
        }
        return overviewAsk;
      }

      loadKpis();
      function loadKpis(){
        const host = document.getElementById('qcKpis');
        if(!host) return;
        overview(true)
          .then(res => {
            if(!host.isConnected) return;
            const s = res.summary;
            host.innerHTML = kpiRow([
              serverKpi(s.spend), serverKpi(s.budget), serverKpi(s.tokens),
              serverKpi(s.api_calls), serverKpi(s.cost_per_1k_tokens), serverKpi(s.capacity),
            ], 190);
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadKpis, 'the cost summary')); });
      }

      tabBar(document.getElementById('qcTabs'),
        [{label:'Overview'},{label:'Quotas'},{label:'Costs'},{label:'Capacity'},{label:'Usage'},{label:'Budgets'},{label:'Forecasting'}],
        renderTab);

      document.getElementById('qcSearch').addEventListener('input', e => {
        searchTerm = e.target.value;
        if(currentTab === 1 || currentTab === 5) renderTab(currentTab);
      });
      document.getElementById('qcExport').addEventListener('click', async () => {
        // The endpoint exports one table at a time, so the button follows the active tab.
        const dataset = currentTab === 1 ? 'quotas' : currentTab === 3 ? 'capacity' : currentTab === 5 ? 'budgets' : 'teams';
        const named = { quotas:'Quotas', capacity:'Capacity', budgets:'Budgets', teams:'Team allocation' };
        try { await API.quota.export({ q: searchTerm, dataset }); toast('success','Export complete', `${named[dataset]} exported to CSV.`); }
        catch (err) { toast('error','Export failed', err.message); }
      });
      document.getElementById('qcCreate').addEventListener('click', createQuota);

      let currentTab = 0;
      function renderTab(i){
        currentTab = i;
        body.innerHTML = '';
        if(i === 0) return overviewTab();
        if(i === 1) return quotasTab();
        if(i === 2) return costsTab();
        if(i === 3) return capacityTab();
        if(i === 4) return usageTab();
        if(i === 5) return budgetsTab();
        return forecastTab();
      }

      // ---- Overview -----------------------------------------------------
      function overviewTab(){
        body.innerHTML = `<div style="display:flex;flex-direction:column;gap:14px">
          <div class="grid g2"><div id="qcSpend"></div><div id="qcServices"></div></div>
          <div class="grid g2"><div id="qcQuotaMini"></div><div id="qcCapMini"></div></div>
          <div id="qcTeams"></div>
          <div id="qcInsights"></div>
        </div>`;

        section(document.getElementById('qcSpend'),
          () => API.quota.usageSeries({ metric: 'spend' }),
          res => {
            const s = (res.series || [])[0];
            if(!s || !s.points.length) return emptyCard('Spend Over Time','No spend recorded in this period');
            return card('Spend Over Time',
              `<div style="padding:8px 4px">${lineChart({
                series:[{ name:s.label, color:s.color||'purple', points:s.points.map(p=>p.value==null?0:p.value), area:true }],
                xLabels:s.points.map(p=>p.label), h:200, zeroBase:true, yFmt:v=>'$'+fmtNum(v) })}</div>`,
              `<span class="faint small">${esc(res.interval||'')}</span>`);
          }, 'the spend chart');

        section(document.getElementById('qcServices'),
          () => overview(),
          res => {
            // The donut's centre is the whole spend, so it sums every slice, not the eight listed.
            const services = res.services || [];
            if(!services.length) return emptyCard('Cost by Service','No service costs recorded');
            const total = services.reduce((a,r)=>a + (r.cost_usd||0), 0);
            const shown = services.slice(0, 8);
            return card('Cost by Service',
              `<div class="donut-wrap">${donut({ segments: services.map(r=>({value:r.cost_usd||0, color:r.color})),
                size:140, thickness:16, centerVal: money(total), centerLabel:'Total Spend' })}
              <div class="legend grow">${shown.map(r=>`<div class="legend-item">
                <span class="sw" style="background:${U.cc(r.color)}"></span>
                <span class="lg-label" style="font-size:11.5px">${esc(r.label)}</span>
                <span class="lg-val">${esc(r.cost_display)}</span>
                <span class="lg-pct">${r.share_percent==null?'—':Math.round(r.share_percent)+'%'}</span></div>`).join('')}</div></div>`);
          }, 'the cost breakdown');

        section(document.getElementById('qcQuotaMini'),
          () => API.quota.quotas.list({ page_size: 6, sort: '-used_value' }),
          page => {
            if(!page.items.length) return emptyCard('Quota Utilization','No quotas defined yet');
            return card('Quota Utilization',
              `<table class="tbl"><thead><tr><th>Quota</th><th>Used</th><th>Limit</th><th>Utilization</th><th>Status</th></tr></thead><tbody>
                ${page.items.map(q=>`<tr style="cursor:default">
                  <td class="cell-main">${esc(q.name)}</td>
                  <td class="num">${esc(q.used_display)}</td>
                  <td class="num dim">${esc(q.limit_display)}</td>
                  <td style="min-width:130px">${barPct(q.utilization_percent, healthColor(q.health))}</td>
                  <td>${statusText(q.health, healthColor(q.health))}</td></tr>`).join('')}</tbody></table>`,
              `<button class="link" data-gotab="1">View All Quotas ${ICONS.arrowRight}</button>`);
          }, 'quota utilisation');

        section(document.getElementById('qcCapMini'),
          () => API.quota.capacity({ page_size: 6 }),
          page => {
            if(!page.items.length) return emptyCard('Capacity Overview','No capacity readings recorded');
            return card('Capacity Overview',
              `<table class="tbl"><thead><tr><th>Resource</th><th>Utilization</th><th>Status</th><th>Trend</th></tr></thead><tbody>
                ${page.items.map(c=>`<tr style="cursor:default">
                  <td><span class="flex" style="gap:8px"><span style="width:15px;display:inline-flex;color:var(--purple-bright)">${ICONS[c.icon]||ICONS.gauge}</span><b style="font-size:12.5px">${esc(c.name)}</b></span></td>
                  <td style="min-width:130px">${barPct(c.utilization_percent, healthColor(c.health))}</td>
                  <td>${statusText(c.status, healthColor(c.health))}</td>
                  <td>${(c.trend||[]).length ? sparkline(c.trend, healthColor(c.health), 96, 26) : dash}</td></tr>`).join('')}</tbody></table>`,
              `<button class="link" data-gotab="3">View Capacity Details ${ICONS.arrowRight}</button>`);
          }, 'capacity');

        section(document.getElementById('qcTeams'),
          () => overview(),
          res => {
            // The first ten, which are also the ten the server drew a sparkline for.
            const teams = (res.teams || []).slice(0, 10);
            if(!teams.length) return emptyCard('Cost & Usage by Team','No team allocation yet — assign owners to agents to see this');
            return card('Cost & Usage by Team',
              `<table class="tbl"><thead><tr><th>Team</th><th>Agents</th><th>Spend</th><th>% of Total</th><th>Tokens</th><th>API Calls</th><th>Avg / 1K</th><th>Trend</th></tr></thead><tbody>
                ${teams.map(t=>`<tr style="cursor:default">
                  <td><span class="flex" style="gap:8px">${avatarHtml(t.team,true)}<b style="font-size:12.5px">${esc(t.team)}</b></span></td>
                  <td class="num">${fmtFull(t.agent_count)}</td>
                  <td class="num">${esc(t.spend_display)}</td>
                  <td class="num dim">${esc(t.share_display)}</td>
                  <td class="num">${esc(t.tokens_display)}</td>
                  <td class="num">${esc(t.api_calls_display)}</td>
                  <td class="num">${t.avg_cost_per_1k_tokens==null?dash:money(t.avg_cost_per_1k_tokens,3)}</td>
                  <td>${(t.trend||[]).length ? sparkline(t.trend,'green',96,26) : dash}</td></tr>`).join('')}</tbody></table>`);
          }, 'team allocation');

        section(document.getElementById('qcInsights'),
          () => overview(),
          res => {
            const insights = (res.insights || []).slice(0, 6);
            if(!insights.length) return emptyCard('Insights','Nothing needs attention right now');
            return card('Insights',
              `<div class="insight-grid">${insights.map(x=>`
                <div class="insight ${esc(x.severity.toLowerCase())}">
                  <span class="insight-ico" style="color:var(--${esc(x.color)})">${ICONS[x.icon]||ICONS.info}</span>
                  <div><div class="insight-title">${esc(x.title)}</div>
                  <div class="insight-body">${esc(x.body)}</div>
                  ${x.potential_savings_usd ? `<div class="insight-save">Potential saving ${money(x.potential_savings_usd)}</div>` : ''}
                  ${(x.recommendations||[]).length ? `<ul class="insight-recs">${x.recommendations.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>` : ''}
                  </div></div>`).join('')}</div>`);
          }, 'insights');
      }

      // Once per screen, not once per visit to the tab: `body` outlives the tab, so
      // a listener added in overviewTab() stacked up and one click switched tabs N times.
      body.addEventListener('click', e => {
        const link = e.target.closest('[data-gotab]');
        if(link){
          const idx = Number(link.dataset.gotab);
          const tab = document.querySelectorAll('#qcTabs .tab')[idx];
          if(tab) tab.click(); else renderTab(idx);
        }
      });

      // ---- Quotas -------------------------------------------------------
      function quotasTab(){
        const table = dataTable({
          columns:[
            { key:'name', label:'Quota', render:r=>entityCell(r.name, r.scope_ref || r.scope, 'gauge', 'purple') },
            { key:'resource', label:'Resource', render:r=>badge(r.resource) },
            { key:'scope', label:'Scope', render:r=>esc(r.scope) },
            { key:'used_value', label:'Used', align:'right', cls:'num', render:r=>esc(r.used_display) },
            { key:'limit_value', label:'Limit', align:'right', cls:'num', render:r=>esc(r.limit_display) },
            { key:'utilization_percent', label:'Utilization', sortable:false, render:r=>barPct(r.utilization_percent, healthColor(r.health)) },
            { key:'enforcement', label:'Enforcement', render:r=>badge(r.enforcement) },
            { key:'period', label:'Period', render:r=>esc(r.period) },
            { key:'resets_label', label:'Resets', sortable:false, render:r=>r.resets_label?esc(r.resets_label):dash },
            { key:'health', label:'Status', sortable:false, render:r=>statusText(r.health, healthColor(r.health)) },
          ],
          rowId:'id', itemName:'quotas', pageSize:10, emptyText:'No quotas defined yet',
          extraParams: searchTerm ? { q: searchTerm } : null,
          filters:[
            {key:'resource', label:'Resource', param:'resource', options:['Tokens','Requests','Cost','Concurrency','Storage'], allLabel:'All Resources'},
            {key:'scope', label:'Scope', param:'scope', options:['Workspace','Agent','Team','Environment'], allLabel:'All Scopes'},
          ],
          source: (p) => API.quota.quotas.list(p),
          exportSource: (p) => API.quota.export(Object.assign({ dataset: 'quotas' }, p)),
          rowActions: r => [
            {label:'Request Increase', icon:'trendUp', onClick:()=>requestIncrease(r)},
            {label:'Edit Quota', icon:'pen', onClick:()=>editQuota(r)},
            {sep:true},
            {label:'Delete Quota', icon:'trash', danger:true, onClick:()=>deleteQuota(r)},
          ],
        });
        body.appendChild(table.filterEl);
        body.appendChild(table.el);
        body._table = table;
      }

      function requestIncrease(r){
        openModal({
          title:'Request Quota Increase', icon:'trendUp',
          body:`<div class="small muted" style="margin-bottom:10px">This opens an approval request. ${esc(r.name)} is currently limited to ${esc(r.limit_display)}.</div>
            <label class="auth-field"><span>New limit</span><input type="number" id="qiLimit" value="${r.limit_value*2}" min="1"></label>
            <label class="auth-field" style="margin-top:10px"><span>Justification</span><input type="text" id="qiWhy" placeholder="Why is more headroom needed?"></label>`,
          footer:[
            {label:'Submit Request', cls:'primary', onClick: async (close, modal) => {
              const limit = Number(modal.querySelector('#qiLimit').value);
              const reason = modal.querySelector('#qiWhy').value.trim();
              close();
              try {
                await API.quota.requestIncrease(r.id, { requested_limit: limit, reason });
                toast('success','Request submitted','It is waiting in Approvals & Audit. Once approved, the new limit is applied automatically.');
                Store.refreshBadges();
              } catch (err) { toast('error','Could not submit', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      function createQuota(){
        if(!Store.session.can('admin')){
          toast('error','Not permitted','Creating a quota requires the admin role.');
          return;
        }
        openModal({
          title:'Create Quota', icon:'gauge',
          body:`<label class="auth-field"><span>Name</span><input type="text" id="cqName" placeholder="Production token ceiling"></label>
            <div class="grid g2" style="margin-top:10px">
              <label class="auth-field"><span>Resource</span><select class="filter-select" id="cqResource" style="height:34px">
                ${METERED_RESOURCES.map(o=>`<option>${o}</option>`).join('')}</select></label>
              <label class="auth-field"><span>Scope</span><select class="filter-select" id="cqScope" style="height:34px">
                ${['Workspace','Agent','Team','Environment'].map(o=>`<option>${o}</option>`).join('')}</select></label>
            </div>
            <label class="auth-field" id="qScopeRefField" style="margin-top:10px;display:none"><span>Scope target</span>
              <input type="text" id="qScopeRef" placeholder="Agent id, team or environment name"></label>
            <div class="grid g2" style="margin-top:10px">
              <label class="auth-field"><span>Limit</span><input type="number" id="cqLimit" value="1000000" min="1"></label>
              <label class="auth-field"><span>Period</span><select class="filter-select" id="cqPeriod" style="height:34px">
                ${['Monthly','Quarterly','Annual'].map(o=>`<option ${o==='Monthly'?'selected':''}>${o}</option>`).join('')}</select></label>
            </div>
            <label class="auth-field" style="margin-top:10px"><span>Enforcement</span><select class="filter-select" id="cqEnf" style="height:34px">
              ${['Block','Warn','Log'].map(o=>`<option>${o}</option>`).join('')}</select></label>`,
          onOpen(modal){
            // Anything narrower than the workspace needs a named target to bind to.
            modal.querySelector('#cqScope').addEventListener('change', e => {
              modal.querySelector('#qScopeRefField').style.display = e.target.value === 'Workspace' ? 'none' : '';
            });
          },
          footer:[
            {label:'Create Quota', cls:'primary', onClick: async (close, modal) => {
              const scope = modal.querySelector('#cqScope').value;
              const scopeRef = modal.querySelector('#qScopeRef').value.trim();
              if(scope !== 'Workspace' && !scopeRef){
                toast('error','Scope target required', `Name the ${scope.toLowerCase()} this quota applies to.`);
                return;
              }
              const resource = modal.querySelector('#cqResource').value;
              const payload = {
                name: modal.querySelector('#cqName').value.trim(),
                resource,
                scope,
                limit_value: Number(modal.querySelector('#cqLimit').value),
                // Left out, the server writes "tokens" whatever the resource is — so a
                // $5,000 ceiling read back, and alerted, as "5K tokens".
                unit: RESOURCE_UNIT[resource] || 'units',
                period: modal.querySelector('#cqPeriod').value,
                enforcement: modal.querySelector('#cqEnf').value,
              };
              if(scope !== 'Workspace') payload.scope_ref = scopeRef;
              try {
                await API.quota.quotas.create(payload);
                close();
                toast('success','Quota created', `${payload.name} is now enforced.`);
                loadKpis();
                if(currentTab === 1) renderTab(1);
              } catch (err) { toast('error','Could not create quota', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      function editQuota(r){
        if(!Store.session.can('admin')){
          toast('error','Not permitted','Editing a quota requires the admin role.');
          return;
        }
        openModal({
          title:'Edit Quota — ' + r.name, icon:'pen',
          body:`<label class="auth-field"><span>Limit</span><input type="number" id="eqLimit" value="${r.limit_value}" min="1"></label>
            <label class="auth-field" style="margin-top:10px"><span>Enforcement</span><select class="filter-select" id="eqEnf" style="height:34px">
              ${['Block','Warn','Log'].map(o=>`<option ${o===r.enforcement?'selected':''}>${o}</option>`).join('')}</select></label>`,
          footer:[
            {label:'Save', cls:'primary', onClick: async (close, modal) => {
              const payload = { limit_value: Number(modal.querySelector('#eqLimit').value),
                                enforcement: modal.querySelector('#eqEnf').value };
              close();
              try {
                await API.quota.quotas.update(r.id, payload);
                toast('success','Quota updated', r.name + ' saved.');
                if(body._table) body._table.refresh();
                loadKpis();
              } catch (err) { toast('error','Could not update quota', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      function deleteQuota(r){
        if(!Store.session.can('admin')){
          toast('error','Not permitted','Deleting a quota requires the admin role.');
          return;
        }
        confirmModal({
          title:'Delete Quota', confirmLabel:'Delete', danger:true,
          msg:`Delete “${r.name}”? Usage against it stops being enforced immediately.`,
          onConfirm: async () => {
            try {
              await API.quota.quotas.remove(r.id);
              toast('success','Quota deleted', r.name + ' removed.');
              if(body._table) body._table.refresh();
              loadKpis();
            } catch (err) { toast('error','Could not delete quota', err.message); }
          },
        });
      }

      // ---- Costs --------------------------------------------------------
      function costsTab(){
        body.innerHTML = `<div style="display:flex;flex-direction:column;gap:14px">
          <div class="grid g2"><div id="qcCostBreak"></div><div id="qcDrivers"></div></div>
          <div id="qcCostService"></div></div>`;

        // Spend going up is the bad direction here, so a rise is the red one.
        const change = (v) => v == null ? dash
          : `<span class="${v > 0 ? 'st-red' : v < 0 ? 'st-green' : 'dim'}">${v > 0 ? '+' : ''}${Number(v).toFixed(1)}%</span>`;
        // The lists arrive whole and heaviest first; a card shows the top of one and says so.
        const topOf = (rows, n) => rows.length > n ? `<span class="faint small">Top ${n} of ${fmtFull(rows.length)}</span>` : '';

        section(document.getElementById('qcCostBreak'),
          () => overview(),
          res => {
            const rows = res.models || [];
            if(!rows.length) return emptyCard('Cost by Model','No model costs recorded');
            /* The row's name is `model`. This read label/key — fields of the service
               and driver rows — so the first column was blank and no cost could be
               put to a model. Tokens, unit cost and the movement against the prior
               period are what this breakdown has that Top Cost Drivers does not. */
            return card('Cost by Model',
              `<table class="tbl"><thead><tr><th>Model</th><th>Tokens</th><th>Cost</th><th>Per 1K</th><th>vs Prior</th><th>Share</th></tr></thead><tbody>
                ${rows.slice(0, 10).map(r=>`<tr style="cursor:default"><td class="cell-main">${esc(r.model)}</td>
                  <td class="num dim">${esc(r.tokens_display)}</td>
                  <td class="num">${r.cost_usd==null?dash:esc(r.cost_display)}</td>
                  <td class="num dim">${r.cost_per_1k_tokens==null?dash:money(r.cost_per_1k_tokens,3)}</td>
                  <td class="num">${change(r.cost_delta_percent)}</td>
                  <td style="min-width:110px">${r.share_percent==null?dash:barPct(r.share_percent,'orange')}</td></tr>`).join('')}</tbody></table>`,
              topOf(rows, 10));
          }, 'the cost breakdown');

        section(document.getElementById('qcDrivers'),
          () => overview(),
          res => {
            const rows = res.drivers || [];
            if(!rows.length) return emptyCard('Top Cost Drivers','Nothing to rank yet');
            return card('Top Cost Drivers',
              `<table class="tbl"><thead><tr><th>Driver</th><th>Cost</th><th>Share</th></tr></thead><tbody>
                ${rows.slice(0, 8).map(r=>`<tr style="cursor:default"><td class="cell-main">${esc(r.label)}</td>
                  <td class="num">${esc(r.cost_display)}</td><td class="num dim">${esc(r.share_display)}</td></tr>`).join('')}</tbody></table>`,
              topOf(rows, 8));
          }, 'cost drivers');

        section(document.getElementById('qcCostService'),
          () => overview(),
          res => {
            const rows = res.services || [];
            if(!rows.length) return emptyCard('Cost by Service','No service costs recorded');
            return card('Cost by Service',
              `<table class="tbl"><thead><tr><th>Service</th><th>Models</th><th>Cost</th><th>Share</th></tr></thead><tbody>
                ${rows.map(r=>`<tr style="cursor:default"><td class="cell-main">${esc(r.label)}</td>
                  <td class="num dim">${fmtFull(r.model_count)}</td>
                  <td class="num">${esc(r.cost_display)}</td>
                  <td style="min-width:120px">${r.share_percent==null?dash:barPct(r.share_percent, 'purple')}</td></tr>`).join('')}</tbody></table>`);
          }, 'service costs');
      }

      // ---- Capacity -----------------------------------------------------
      function capacityTab(){
        const table = dataTable({
          columns:[
            { key:'name', label:'Resource', render:r=>`<span class="flex" style="gap:8px"><span style="width:15px;display:inline-flex;color:var(--purple-bright)">${ICONS[r.icon]||ICONS.gauge}</span><b style="font-size:12.5px">${esc(r.name)}</b></span>` },
            { key:'resource_type', label:'Type', render:r=>badge(r.resource_type) },
            { key:'region', label:'Region', render:r=>r.region?esc(r.region):dash },
            { key:'provisioned', label:'Provisioned', align:'right', cls:'num', render:r=>`${fmtNum(r.provisioned)} <span class="faint">${esc(r.unit)}</span>` },
            { key:'used', label:'Used', align:'right', cls:'num', render:r=>fmtNum(r.used) },
            { key:'headroom', label:'Headroom', align:'right', cls:'num', render:r=>`${fmtNum(r.headroom)} <span class="faint">(${pct(r.headroom_percent,0)})</span>` },
            { key:'utilization_percent', label:'Utilization', render:r=>barPct(r.utilization_percent, healthColor(r.health)) },
            { key:'status', label:'Status', render:r=>statusText(r.status, healthColor(r.health)) },
            { key:'trend', label:'Trend', sortable:false, render:r=>(r.trend||[]).length?sparkline(r.trend, healthColor(r.health), 96, 26):dash },
            { key:'measured_at', label:'Measured', render:r=>`<span class="dim nowrap">${when(r.measured_at)}</span>` },
          ],
          rowId:'name', itemName:'resources', pageSize:15, emptyText:'No capacity readings recorded',
          source: (p) => API.quota.capacity(p),
        });
        body.appendChild(table.el);
      }

      // ---- Usage --------------------------------------------------------
      function usageTab(){
        body.innerHTML = `<div class="grid g2" id="qcUsageCharts"></div>`;
        const host = document.getElementById('qcUsageCharts');
        [['tokens','Tokens Over Time'],['spend','Cost Over Time']].forEach(([metric,title]) => {
          const slot = document.createElement('div');
          host.appendChild(slot);
          section(slot, () => API.quota.usageSeries({ metric }), res => {
            const s = (res.series || [])[0];
            if(!s || !s.points.length) return emptyCard(title, 'No usage in this period');
            return card(title, `<div style="padding:8px 4px">${lineChart({
              series:[{ name:s.label, color:s.color||'purple', points:s.points.map(p=>p.value==null?0:p.value), area:true }],
              xLabels:s.points.map(p=>p.label), h:210, zeroBase:true })}</div>`);
          }, title.toLowerCase());
        });
      }

      // ---- Budgets ------------------------------------------------------
      function budgetsTab(){
        body.innerHTML = `<div class="flex" style="justify-content:flex-end;gap:8px;margin-bottom:12px">
            <button class="btn" id="qcRefreshBudgets">${ICONS.refresh}Re-measure Spend</button>
            <button class="btn primary" id="qcNewBudget">${ICONS.plus}New Budget</button>
          </div><div id="qcBudgetTable"></div>`;

        const table = dataTable({
          columns:[
            { key:'name', label:'Budget', render:r=>entityCell(r.name, r.scope_ref || r.scope, 'creditCard', 'blue') },
            { key:'period', label:'Period', sortable:false, render:r=>esc(r.period) },
            { key:'spent_usd', label:'Spent', align:'right', cls:'num', render:r=>esc(r.spent_display) },
            { key:'amount_usd', label:'Budget', align:'right', cls:'num', render:r=>esc(r.amount_display) },
            { key:'utilization_percent', label:'Utilization', sortable:false, render:r=>barPct(r.utilization_percent, healthColor(r.health)) },
            { key:'projected_spend_usd', label:'Projected', align:'right', cls:'num', sortable:false, render:r=>r.projected_spend_usd==null?dash:`${money(r.projected_spend_usd)}${r.on_pace_to_breach?' <span class="st-red">over</span>':''}` },
            { key:'warn_threshold_percent', label:'Thresholds', sortable:false, render:r=>`<span class="faint">${r.warn_threshold_percent}% / ${r.hard_threshold_percent}%</span>` },
            { key:'resets_label', label:'Resets', sortable:false, render:r=>r.resets_label?esc(r.resets_label):dash },
            // Health is measured against a live period. A budget whose period has lapsed,
            // or that is switched off, is not "Healthy" — it is not being held to anything.
            { key:'health', label:'Status', sortable:false, render:r=>(r.status === 'Expired' || r.status === 'Disabled')
                ? statusText(r.status, 'gray') : statusText(r.health, healthColor(r.health)) },
          ],
          rowId:'id', itemName:'budgets', pageSize:10, emptyText:'No budgets set',
          extraParams: searchTerm ? { q: searchTerm } : null,
          source: (p) => API.quota.budgets.list(p),
          rowActions: r => [
            {label:'Edit Thresholds', icon:'settings', onClick:()=>editThresholds(r)},
            {sep:true},
            {label:'Delete Budget', icon:'trash', danger:true, onClick:()=>{
              if(!Store.session.can('admin')){
                toast('error','Not permitted','Deleting a budget requires the admin role.');
                return;
              }
              confirmModal({
                title:'Delete Budget', confirmLabel:'Delete', danger:true,
                msg:`Delete “${r.name}”? Spend against it stops being tracked.`,
                onConfirm: async () => {
                  try { await API.quota.budgets.remove(r.id); toast('success','Budget deleted', r.name+' removed.'); table.refresh(); }
                  catch (err) { toast('error','Could not delete budget', err.message); }
                } });
            }},
          ],
        });
        document.getElementById('qcBudgetTable').appendChild(table.el);

        document.getElementById('qcRefreshBudgets').addEventListener('click', async (e) => {
          if(!Store.session.can('operator')){
            toast('error','Not permitted','Re-measuring spend requires the operator role.');
            return;
          }
          e.currentTarget.disabled = true;
          try {
            // Re-measuring is what raises a budget's threshold alert, so it goes
            // through Store.mutate: the sidebar's Alerts badge is re-read as soon
            // as the pass lands, instead of on the app shell's next timer tick.
            const res = await Store.mutate(() => API.quota.refreshBudgets(), { event:'alerts:changed' });
            toast('success','Budgets re-measured', res && res.message ? res.message : 'Spend re-read and thresholds re-evaluated.');
            table.refresh(); loadKpis();
          } catch (err) { toast('error','Could not re-measure', err.message); }
          finally { e.currentTarget.disabled = false; }
        });

        document.getElementById('qcNewBudget').addEventListener('click', () => {
          if(!Store.session.can('admin')){
            toast('error','Not permitted','Creating a budget requires the admin role.');
            return;
          }
          openModal({
            title:'New Budget', icon:'creditCard',
            body:`<label class="auth-field"><span>Name</span><input type="text" id="nbName" placeholder="Production monthly"></label>
              <div class="grid g2" style="margin-top:10px">
                <label class="auth-field"><span>Amount (USD)</span><input type="number" id="nbAmount" value="5000" min="1"></label>
                <label class="auth-field"><span>Period</span><select class="filter-select" id="nbPeriod" style="height:34px">
                  ${['Monthly','Quarterly','Annual'].map(o=>`<option ${o==='Monthly'?'selected':''}>${o}</option>`).join('')}</select></label>
              </div>
              <div class="grid g2" style="margin-top:10px">
                <label class="auth-field"><span>Warn at (%)</span><input type="number" id="nbWarn" value="80" min="1" max="100"></label>
                <label class="auth-field"><span>Hard stop at (%)</span><input type="number" id="nbHard" value="100" min="1" max="200"></label>
              </div>`,
            footer:[
              {label:'Create Budget', cls:'primary', onClick: async (close, modal) => {
                const payload = {
                  name: modal.querySelector('#nbName').value.trim(),
                  amount_usd: Number(modal.querySelector('#nbAmount').value),
                  period: modal.querySelector('#nbPeriod').value,
                  warn_threshold_percent: Number(modal.querySelector('#nbWarn').value),
                  hard_threshold_percent: Number(modal.querySelector('#nbHard').value),
                  scope: 'Workspace',
                };
                close();
                try {
                  // The 201 is already measured: a budget set below what has been spent
                  // opens past its threshold, with the alert raised, and should say so.
                  const created = await API.quota.budgets.create(payload);
                  const breached = created && (created.status === 'Warning' || created.status === 'Exceeded');
                  toast(breached ? 'warn' : 'success', 'Budget created', breached
                    ? `${payload.name} opens at ${created.spent_display} of ${created.amount_display} — ${created.status.toLowerCase()} already, and an alert has been raised.`
                    : `${payload.name} is now tracked.`);
                  table.refresh(); loadKpis();
                } catch (err) { toast('error','Could not create budget', err.message); }
              }},
              {label:'Cancel'},
            ],
          });
        });
      }

      function editThresholds(r){
        if(!Store.session.can('admin')){
          toast('error','Not permitted','Editing thresholds requires the admin role.');
          return;
        }
        openModal({
          title:'Edit Thresholds — ' + r.name, icon:'settings',
          body:`<div class="small muted" style="margin-bottom:10px">Crossing a threshold raises an alert against this budget.</div>
            <div class="grid g2">
              <label class="auth-field"><span>Warn at (%)</span><input type="number" id="etWarn" value="${r.warn_threshold_percent}" min="1" max="100"></label>
              <label class="auth-field"><span>Hard stop at (%)</span><input type="number" id="etHard" value="${r.hard_threshold_percent}" min="1" max="200"></label>
            </div>`,
          footer:[
            {label:'Save', cls:'primary', onClick: async (close, modal) => {
              const payload = { warn_threshold_percent: Number(modal.querySelector('#etWarn').value),
                                hard_threshold_percent: Number(modal.querySelector('#etHard').value) };
              close();
              try {
                await API.quota.budgets.update(r.id, payload);
                toast('success','Thresholds updated', r.name + ' saved.');
                renderTab(5);
              } catch (err) { toast('error','Could not update thresholds', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      // ---- Forecasting --------------------------------------------------
      function forecastTab(){
        body.innerHTML = `<div id="qcForecast"></div>`;
        section(document.getElementById('qcForecast'), () => API.quota.forecast(), f => {
          /* One list, and each day carries a measured value or a projected one,
             never both — so each series is mostly nulls, which have to stay gaps
             (see gapLineChart). The projection picks up where measurement stops:
             the dashed line is started on the last measured day so it continues
             the solid one instead of floating beside it. With nothing projected
             there is no Forecast series to draw, and no legend entry for one. */
          const points = f.points || [];
          const actual = points.map(p => p.actual_usd);
          const forecast = points.map(p => p.forecast_usd);
          const firstProjected = forecast.findIndex(v => v != null);
          if(firstProjected > 0 && actual[firstProjected - 1] != null) forecast[firstProjected - 1] = actual[firstProjected - 1];
          const series = [{ name:'Actual', color:'purple', points: actual, area:true }];
          if(firstProjected >= 0) series.push({ name:'Forecast', color:'gray', dashed:true, points: forecast });
          const measured = actual.some(v => v != null) || firstProjected >= 0;
          const chart = measured
            ? gapLineChart({ series, xLabels: points.map(p=>p.label), h:230, yFmt:v=>'$'+fmtNum(v) })
            : `<div class="empty-state">${ICONS.chart}<div class="es-title">Not enough history to forecast yet</div></div>`;

          return card('Spend Forecast', `${chart}
            <div class="legend inline" style="margin-top:6px">${measured ? series.map(s=>
              `<span class="legend-item"><span class="sw" style="background:${U.cc(s.color)}"></span><span class="lg-label">${s.name}</span></span>`).join('') : ''}
            </div>
            <div class="grid g3" style="margin-top:14px">
              ${kv([['Method', esc(f.method)],['Days elapsed', String(f.days_elapsed)],['Days remaining', String(f.days_remaining)]])}
              ${kv([['Observed spend', money(f.observed_spend_usd)],['Projected spend', money(f.projected_spend_usd)],['Budget', money(f.budget_usd)]])}
              ${kv([['Projected utilisation', pct(f.projected_utilization_percent)],
                    ['Confidence', `±${money(f.confidence_interval_usd)} at ${pct(f.confidence_level,0)}`],
                    ['Daily growth', f.daily_growth_usd==null?dash:money(f.daily_growth_usd,2)]])}
            </div>
            ${f.highest_growth_driver ? `<div class="scan-note" style="margin-top:12px">${ICONS.trendUp} Fastest-growing driver: <b>${esc(f.highest_growth_driver)}</b>, up ${money(f.highest_growth_delta_usd,2)} over the period.</div>` : ''}
            ${f.anomalies_detected ? `<div class="scan-note" style="margin-top:8px">${ICONS.alert} ${f.anomalies_detected} spend anomal${f.anomalies_detected===1?'y':'ies'} detected${(f.anomaly_days||[]).length?` on ${f.anomaly_days.map(esc).join(', ')}`:''}.</div>` : ''}`,
            `<span class="badge bg-purple">Projected ${f.projected_spend_usd==null?'—':esc(money(f.projected_spend_usd))}</span>`);
        }, 'the forecast');
      }

      // tabBar only reports clicks. Without this the screen opened on its KPI row
      // and a tab bar with nothing under them until a tab was clicked.
      renderTab(0);

      // Approving an increase raises the quota's ceiling on the server. When that
      // decision lands while this screen is up, re-read the table rather than go
      // on showing the old limit.
      const onApproval = () => { if(currentTab === 1 && body._table) body._table.refresh(); };
      Store.on('approvals:changed', onApproval);
      this.cleanup = () => Store.off('approvals:changed', onApproval);
    },
  };

  /* ================= MEMORY & STATE MANAGEMENT ================= */
  SCREENS['memory'] = {
    title:'Memory & State Management',
    render(main){
      main.innerHTML = `
        ${pageHead({title:'Memory & State Management', sub:'Govern agent memory, session state and conversation context across environments.',
          actions:`${searchBox('msSearch','Search memory stores…')}
          <button class="btn" id="msExport">${ICONS.download}Export</button>
          <button class="btn primary" id="msCreate">${ICONS.plus}Create Memory Store</button>`})}
        <div id="msKpis">${kpiSkeleton(['Total Memory Stores','Active Sessions','Stored Memories','Avg. Retrieval Latency','State Sync Success','Expired / Purged'])}</div>
        <div id="msTabs"></div>
        <div id="msBody"></div>`;

      const body = document.getElementById('msBody');
      let table = null;

      loadKpis();
      function loadKpis(){
        const host = document.getElementById('msKpis');
        if(!host) return;
        API.memory.summary()
          .then(s => {
            if(!host.isConnected) return;
            host.innerHTML = kpiRow([
              {label:'Total Memory Stores', value:fmtFull(s.stores), icon:'database', color:'purple',
               sub:`${s.active_stores} active · ${s.paused_stores} paused · ${s.degraded_stores} degraded`},
              {label:'Active Sessions', value:fmtFull(s.active_sessions), icon:'activity', color:'green',
               sub:`in the last ${s.active_session_window_hours}h`},
              {label:'Stored Memories', value:fmtNum(s.stored_memories), icon:'brain', color:'blue',
               sub:`${s.thread_backed_stores} store(s) backed by conversation threads`},
              {label:'Avg. Retrieval Latency', value:s.avg_retrieval_latency_ms==null?'—':Math.round(s.avg_retrieval_latency_ms)+'ms', icon:'clock', color:'amber'},
              {label:'State Sync Success', value:pct(s.state_sync_success_percent), icon:'refresh', color:'cyan'},
              {label:'Expired / Purged', value:fmtNum(s.expired_purged_records), icon:'trash', color:'red',
               sub:`${s.purge_runs} purge run(s) in ${s.purge_window_days} days`},
            ]);
          })
          .catch(err => { host.innerHTML=''; host.appendChild(screenError(err, loadKpis, 'the memory summary')); });
      }

      tabBar(document.getElementById('msTabs'),
        [{label:'Memory Stores'},{label:'Sessions'},{label:'Agent State'},{label:'Conversation State'},{label:'Retention Policies'},{label:'Backups'}],
        renderTab);

      document.getElementById('msSearch').addEventListener('input', e => { if(table) table.search(e.target.value); });
      document.getElementById('msExport').addEventListener('click', async () => {
        /* The file is always the stores list, so only the Memory Stores tab's
           query belongs to it. Another tab's sort key (agent_name, last_activity_at)
           is one the export refuses with 422, and its search text — typed about
           sessions or backups — would silently filter the stores. */
        try { await API.memory.export(currentTab === 0 && table ? table.params() : {}); toast('success','Export complete','Memory stores exported to CSV.'); }
        catch (err) { toast('error','Export failed', err.message); }
      });
      document.getElementById('msCreate').addEventListener('click', createStore);

      let currentTab = 0;
      function renderTab(i){
        currentTab = i;
        body.innerHTML = '';
        table = null;
        if(i === 0) return storesTab();
        if(i === 1) return simpleTab('sessions');
        if(i === 2) return simpleTab('agent-state');
        if(i === 3) return simpleTab('conversations');
        if(i === 4) return retentionTab();
        return backupsTab();
      }

      function storesTab(){
        body.innerHTML = `<div class="with-inspector" id="msLayout"><div id="msTableWrap"></div><div class="inspector" id="msInspector"></div></div>`;
        table = dataTable({
          columns:[
            { key:'name', label:'Memory Store', render:r=>entityCell(r.name, r.backend || r.store_type, 'database',
                r.store_type==='Vector'?'purple':r.store_type==='Session'?'cyan':'blue') },
            { key:'store_type', label:'Type', render:r=>badge(r.store_type) },
            { key:'environment', label:'Environment', render:r=>badge(r.environment) },
            /* Nullable, all three. A thread-backed store's records and sessions are
               counted live and are null when telemetry cannot answer; its usage is
               always null, because nothing reports a capacity for it. Null is a
               dash — a 0% bar would be a measurement nobody took (and barPct(null)
               throws, which took the whole table down with it). */
            { key:'record_count', label:'Records', align:'right', cls:'num', render:r=>r.record_count==null?dash:fmtNum(r.record_count) },
            { key:'usage_percent', label:'Usage', render:r=>usageBar(r.usage_percent) },
            { key:'active_session_count', label:'Sessions', align:'right', cls:'num', render:r=>r.active_session_count==null?dash:fmtFull(r.active_session_count) },
            { key:'avg_retrieval_latency_ms', label:'Latency', align:'right', cls:'num', render:r=>r.avg_retrieval_latency_ms==null?dash:Math.round(r.avg_retrieval_latency_ms)+'ms' },
            { key:'retention_policy', label:'Retention', render:r=>r.retention_policy?esc(r.retention_policy):dash },
            { key:'status', label:'Status', render:r=>statusText(r.status) },
          ],
          rowId:'id', itemName:'memory stores', pageSize:10, emptyText:'No memory stores yet',
          filters:[
            {key:'store_type', label:'Type', param:'type', options:['Conversation','Vector','Key-Value','Session','Long-term'], allLabel:'All Types'},
            {key:'environment', label:'Environment', param:'environment', options:['Production','Staging','Development'], allLabel:'All Environments'},
            {key:'status', label:'Status', param:'status', options:['Active','Paused','Degraded'], allLabel:'All'},
          ],
          source: (p) => API.memory.list(p),
          exportSource: (p) => API.memory.export(p),
          autoSelectFirst: true,
          onSelect: showStore,
          rowActions: r => [
            {label:'View Records', icon:'database', onClick:()=>viewRecords(r)},
            {label:'Edit Store', icon:'pen', onClick:()=>editStore(r)},
            {label: r.status === 'Paused' ? 'Resume Store' : 'Pause Store', icon: r.status === 'Paused' ? 'play' : 'pause', onClick:()=>togglePause(r)},
            {label:'Update Retention Policy', icon:'settings', onClick:()=>updateRetention(r)},
            {label:'Create Backup', icon:'history', onClick:()=>createBackup(r)},
            {label:'Restore from Backup', icon:'refresh', onClick:()=>restoreBackup(r)},
            {sep:true},
            {label:'Purge Data', icon:'trash', danger:true, onClick:()=>purgeStore(r)},
            {label:'Delete Store', icon:'trash', danger:true, onClick:()=>deleteStore(r)},
          ],
        });
        const wrap = document.getElementById('msTableWrap');
        wrap.appendChild(table.filterEl);
        wrap.appendChild(table.el);
      }

      const usageBar = (v) => v == null ? dash : barPct(v, v>=85?'red':v>=70?'amber':'green');

      function editStore(r){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Editing a memory store requires the operator role.');
          return;
        }
        openModal({
          title:'Edit Store — ' + r.name, icon:'pen',
          body:`<label class="auth-field"><span>Name</span><input type="text" id="edName" maxlength="160" value="${esc(r.name)}"></label>
            <div class="grid g2" style="margin-top:10px">
              <label class="auth-field"><span>Backend</span><input type="text" id="edBackend" maxlength="80" value="${esc(r.backend||'')}" placeholder="e.g. Redis, pgvector"></label>
              <label class="auth-field"><span>Environment</span><select class="filter-select" id="edEnv" style="height:34px">
                ${['Production','Staging','UAT','Development','QA','Sandbox','DR'].map(o=>`<option ${o===r.environment?'selected':''}>${o}</option>`).join('')}</select></label>
            </div>
            ${r.thread_backed ? `<div class="small muted" style="margin-top:8px">Agents are bound to this store by its name. Renaming it moves the ${r.bound_agents == null ? '' : fmtFull(r.bound_agents) + ' '}agent(s) bound to it along, so their name has to fit an agent's memory policy (48 characters).</div>` : ''}`,
          footer:[
            {label:'Save', cls:'primary', onClick: async (close, modal) => {
              const name = modal.querySelector('#edName').value.trim();
              if(!name){ toast('error','Name required','Give the store a name.'); return; }
              // Only what changed, under the version that was read: someone else's edit is a 409, not an overwrite.
              const payload = { expected_updated_at: r.updated_at };
              if(name !== r.name) payload.name = name;
              const backend = modal.querySelector('#edBackend').value.trim();
              if(backend && backend !== (r.backend || '')) payload.backend = backend;
              const environment = modal.querySelector('#edEnv').value;
              if(environment !== r.environment) payload.environment = environment;
              if(Object.keys(payload).length === 1){ close(); return; }
              try {
                await API.memory.update(r.id, payload);
                close();
                toast('success','Store updated', name + ' saved.');
                if(table) table.refresh();
              } catch (err) { toast('error','Could not update store', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      async function togglePause(r){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Pausing a memory store requires the operator role.');
          return;
        }
        const pausing = r.status !== 'Paused';
        try {
          await API.memory.update(r.id, { status: pausing ? 'Paused' : 'Active', expected_updated_at: r.updated_at });
          toast('success', pausing ? 'Store paused' : 'Store resumed',
            pausing ? `${r.name} is paused; it cannot be purged until it is resumed.` : `${r.name} is active again.`);
          if(table) table.refresh();
          loadKpis();
        } catch (err) { toast('error', pausing ? 'Could not pause' : 'Could not resume', err.message); }
      }

      function deleteStore(r){
        if(!Store.session.can('admin')){
          toast('error','Not permitted','Deleting a memory store requires the admin role.');
          return;
        }
        confirmModal({
          title:'Delete Memory Store', confirmLabel:'Delete', danger:true,
          msg:`Delete “${r.name}”? This removes the store from the registry. It deletes no conversation records — purge first if they should go.`,
          onConfirm: async () => {
            try {
              await API.memory.remove(r.id);
              toast('success','Store deleted', r.name + ' removed.');
              renderTab(0);
              loadKpis();
            } catch (err) {
              // 412 names the agents still bound to it; 409 means a purge of it is running.
              toast('error','Could not delete store', err.message);
            }
          },
        });
      }

      function showStore(r){
        const insp = document.getElementById('msInspector');
        if(!insp || !r) return;
        document.getElementById('msLayout').classList.remove('collapsed');
        insp.innerHTML = `
          <div class="insp-head"><div><div class="insp-title">${esc(r.name)}</div>
            <div class="insp-sub">${esc(r.store_type)} · ${esc(r.environment)}</div></div>
            <button class="icon-btn insp-close" id="msInspClose">${ICONS.x}</button></div>
          ${inspSection('Store','database', kv([
            ['Status', statusText(r.status)],
            ['Backend', r.backend?esc(r.backend):dash],
            ['Records', r.record_count==null?dash:fmtNum(r.record_count)],
            ['Usage', usageBar(r.usage_percent)],
            ['Thread-backed', r.thread_backed?'<span class="st-green">Yes</span>':'No'],
            // What a thread-backed store governs is exactly the agents that name it.
            r.thread_backed ? ['Bound agents', r.bound_agents==null?dash:fmtFull(r.bound_agents)] : null,
          ]))}
          ${r.thread_backed && r.bound_agents === 0 ? `<div class="scan-note" style="margin:0 0 12px">${ICONS.info} No agent is bound to this store, so it has no records to list, count or purge. Set an agent's Memory Policy to this store's name to bind it.</div>` : ''}
          ${inspSection('Activity','activity', kv([
            ['Active sessions', r.active_session_count==null?dash:fmtFull(r.active_session_count)],
            ['Avg retrieval latency', r.avg_retrieval_latency_ms==null?dash:Math.round(r.avg_retrieval_latency_ms)+'ms'],
            ['Last updated', when(r.last_updated_at)],
          ]))}
          ${inspSection('Retention & Backups','history', kv([
            ['Policy', r.retention_policy?esc(r.retention_policy):dash],
            ['Retention days', r.retention_days==null?dash:String(r.retention_days)],
            ['Last backup', when(r.last_backup_at)],
            ['Backups held', fmtFull(r.backup_count)],
          ]))}
          ${inspSection('Ownership','user', kv([
            ['Owner', r.owner_name?esc(r.owner_name):dash],
            ['Team', r.owner_team?esc(r.owner_team):dash],
          ]))}`;
        insp.querySelector('#msInspClose').addEventListener('click', ()=>document.getElementById('msLayout').classList.add('collapsed'));
      }

      /** The three read-only tabs share one shape: a server table over a sub-resource. */
      function simpleTab(kind){
        const specs = {
          'sessions': {
            fetch: (p) => API.memory.sessions(p), name:'sessions', empty:'No sessions recorded',
            columns:[
              { key:'session_id', label:'Session', sortable:false, render:r=>`<span class="mono">${esc(String(r.session_id).slice(0,18))}…</span>` },
              { key:'agent_name', label:'Agent', sortable:false, render:r=>r.agent_id?`<span class="link" data-nav="agent/${esc(r.agent_id)}">${esc(r.agent_name)}</span>`:esc(r.agent_name||'—') },
              { key:'user', label:'User', sortable:false, render:r=>r.user?esc(r.user):dash },
              { key:'turns', label:'Turns', align:'right', cls:'num', sortable:false, render:r=>fmtFull(r.turns) },
              { key:'duration_ms', label:'Duration', align:'right', cls:'num', sortable:false, render:r=>r.duration_ms==null?dash:(r.duration_ms/1000).toFixed(1)+'s' },
              { key:'started_at', label:'Started', sortable:false, render:r=>`<span class="dim nowrap">${when(r.started_at)}</span>` },
              { key:'last_activity_at', label:'Last Activity', sortable:false, render:r=>`<span class="dim nowrap">${when(r.last_activity_at)}</span>` },
              { key:'state', label:'State', sortable:false, render:r=>statusText(r.state) },
            ],
            rowId:'session_id',
          },
          'agent-state': {
            fetch: (p) => API.memory.agentState(p), name:'agent state records', empty:'No agent state recorded',
            columns:[
              { key:'agent_name', label:'Agent', render:r=>r.agent_id?`<span class="link" data-nav="agent/${esc(r.agent_id)}">${esc(r.agent_name||r.agent_id)}</span>`:esc(r.agent_name||'—') },
              /* An agent is bound to a store by naming it in its Memory Policy. A policy
                 that names no store resolves to nothing, and the server still answers
                 "Synced" for it — a state nobody measured, since there is no store to
                 be in step with. So the policy is shown for what it is, and the sync
                 state of an agent with no store is a dash. */
              { key:'state_store', label:'Store', sortable:false, render:r=>r.state_store?esc(r.state_store)
                  :r.memory_policy?`<span class="faint" title="No memory store has this name">${esc(r.memory_policy)} — no store by this name</span>`:dash },
              { key:'session_count', label:'Sessions', align:'right', cls:'num', sortable:false, render:r=>r.session_count==null?dash:fmtFull(r.session_count) },
              { key:'last_activity_at', label:'Last Activity', render:r=>`<span class="dim nowrap">${when(r.last_activity_at)}</span>` },
              { key:'sync_state', label:'Sync State', sortable:false, render:r=>(r.state_store || r.sync_state !== 'Synced')?statusText(r.sync_state):dash },
            ],
            rowId:'agent_id',
          },
          'conversations': {
            fetch: (p) => API.memory.conversations(p), name:'conversations', empty:'No conversation state recorded',
            columns:[
              { key:'conversation_id', label:'Conversation', sortable:false, render:r=>`<span class="mono">${esc(String(r.conversation_id||'').slice(0,18))}…</span>` },
              { key:'agent_name', label:'Agent', sortable:false, render:r=>esc(r.agent_name||'—') },
              { key:'messages', label:'Messages', align:'right', cls:'num', sortable:false, render:r=>fmtFull(r.messages) },
              { key:'context_tokens', label:'Context Tokens', align:'right', cls:'num', sortable:false, render:r=>r.context_tokens==null?dash:fmtNum(r.context_tokens) },
              { key:'retention_policy', label:'Retention', sortable:false, render:r=>r.retention_policy?esc(r.retention_policy):dash },
              { key:'last_activity_at', label:'Last Activity', sortable:false, render:r=>`<span class="dim nowrap">${when(r.last_activity_at)}</span>` },
              { key:'expires_at', label:'Expires', sortable:false, render:r=>`<span class="dim nowrap">${when(r.expires_at)}</span>` },
            ],
            rowId:'conversation_id',
          },
        };
        const spec = specs[kind];
        const t = dataTable({
          columns: spec.columns, rowId: spec.rowId, itemName: spec.name,
          pageSize: 15, emptyText: spec.empty, source: spec.fetch,
        });
        body.appendChild(t.el);
        table = t;
      }

      function retentionTab(){
        section(body, () => API.memory.retentionPolicies(), rows => {
          if(!rows.length) return emptyCard('Retention Policies','No retention policies configured');
          return card('Retention Policies',
            `<table class="tbl"><thead><tr><th>Policy</th><th>Retention</th><th>Applies To</th><th>Stores</th><th>Records</th><th>Status</th></tr></thead><tbody>
              ${rows.map(p=>`<tr style="cursor:default">
                <td class="cell-main">${esc(p.policy)}</td>
                <td class="num">${p.retention_days==null?'Indefinite':p.retention_days+' days'}</td>
                <td class="dim">${(p.applies_to||[]).map(esc).join(', ')||'—'}</td>
                <td class="num">${fmtFull(p.store_count)}</td>
                <td class="num">${fmtNum(p.record_count)}</td>
                <td>${statusText(p.status)}</td></tr>`).join('')}</tbody></table>`);
        }, 'retention policies');
      }

      function backupsTab(){
        const t = dataTable({
          columns:[
            { key:'created_at', label:'Taken', sortable:false, render:r=>`<span class="dim nowrap">${when(r.created_at)}</span>` },
            { key:'store_name', label:'Store', sortable:false, render:r=>esc(r.store_name||'—') },
            { key:'kind', label:'Kind', sortable:false, render:r=>badge(r.kind) },
            // Counted when the snapshot was taken — not held by it, which the next column says.
            { key:'record_count', label:'Threads Counted', align:'right', cls:'num', sortable:false, render:r=>fmtNum(r.record_count) },
            { key:'threads_captured', label:'Threads Captured', sortable:false, render:r=>r.threads_captured?'<span class="st-green">Yes</span>':'No' },
            { key:'captured', label:'Holds', sortable:false, render:r=>(r.captured||[]).length?`<span class="dim">${r.captured.map(f=>esc(String(f).replace(/_/g,' '))).join(', ')}</span>`:dash },
            // Null on every new row: a snapshot exports nothing, so there is nothing to size.
            { key:'payload_bytes', label:'Size', align:'right', cls:'num', sortable:false, render:r=>r.payload_bytes==null?dash:byteSize(r.payload_bytes) },
            { key:'created_by', label:'By', sortable:false, render:r=>r.created_by?esc(r.created_by):dash },
            { key:'status', label:'Status', sortable:false, render:r=>statusText(r.status) },
          ],
          rowId:'id', itemName:'backups', pageSize:15, emptyText:'No backups taken yet',
          source: (p) => API.memory.backups(p),
        });
        body.appendChild(t.el);
        table = t;
      }

      function viewRecords(r){
        openModal({
          title:'Records — ' + r.name, icon:'database', wide:true,
          body:`<div class="card-loading" style="height:200px"></div>`,
          footer:[{label:'Close'}],
          onOpen(modal){
            const host = modal.querySelector('.modal-body');
            API.memory.records(r.id, { page_size: 25 })
              .then(page => {
                if(!page.items.length){
                  host.innerHTML = `<div class="empty-state">${ICONS.database}<div class="es-title">This store holds no records yet</div></div>`;
                  return;
                }
                const keys = Object.keys(page.items[0]).slice(0, 6);
                host.innerHTML = `<div class="small muted" style="margin-bottom:8px">${fmtNum(page.total)} record(s); showing the first ${page.items.length}.</div>
                  <table class="tbl"><thead><tr>${keys.map(k=>`<th>${esc(k.replace(/_/g,' '))}</th>`).join('')}</tr></thead><tbody>
                  ${page.items.map(row=>`<tr style="cursor:default">${keys.map(k=>{
                    const v = row[k];
                    const text = v == null ? '—' : (typeof v === 'object' ? JSON.stringify(v) : String(v));
                    return `<td class="dim" style="max-width:220px;overflow:hidden;text-overflow:ellipsis">${esc(text.slice(0,140))}</td>`;
                  }).join('')}</tr>`).join('')}</tbody></table>`;
              })
              .catch(err => { host.innerHTML=''; host.appendChild(screenError(err, null, 'these records')); });
          },
        });
      }

      function updateRetention(r){
        if(!Store.session.can('admin')){
          toast('error','Not permitted','Updating a retention policy requires the admin role.');
          return;
        }
        /* A store made from this console is labelled from its days ("90 days"), and
           the server only derives that label when none is sent. Pre-filled and
           sent back, the old label was stored beside the new number: the table,
           the Retention Policies tab and the CSV went on reading "90 days" while
           the purge enforced 7. A label that is just a day count is therefore
           never sent — the server writes the one that matches the days — and the
           field is kept for a name someone actually chose ("Legal hold"). */
        const dayCount = /^\d+\s*days?$/i;
        const custom = r.retention_policy && !dayCount.test(r.retention_policy.trim()) ? r.retention_policy : '';
        openModal({
          title:'Update Retention Policy — ' + r.name, icon:'settings',
          body:`<label class="auth-field"><span>Retention (days)</span>
              <input type="number" id="urDays" value="${r.retention_days == null ? '' : r.retention_days}" min="1" placeholder="e.g. 90"></label>
            <label class="auth-field" style="margin-top:10px"><span>Policy name (optional)</span><input type="text" id="urPolicy" maxlength="48" value="${esc(custom)}" placeholder="Blank: named after the days, e.g. “90 days”"></label>
            <div class="small muted" style="margin-top:8px">Records older than the retention window become eligible for purge. Changing this does not delete anything on its own.</div>`,
          footer:[
            {label:'Save Policy', cls:'primary', onClick: async (close, modal) => {
              const days = Number(modal.querySelector('#urDays').value);
              if(!Number.isInteger(days) || days < 1){ toast('error','Retention required','Retention is a whole number of days, at least 1.'); return; }
              const label = modal.querySelector('#urPolicy').value.trim();
              const payload = { retention_policy: label && !dayCount.test(label) ? label : null,
                                retention_days: days };
              close();
              try {
                // Counts do not move with a policy, so the KPI row is left alone.
                await API.memory.retention(r.id, payload);
                toast('success','Retention updated', `${r.name} now keeps records for ${days} day${days === 1 ? '' : 's'}.`);
                if(table) table.refresh();
              } catch (err) { toast('error','Could not update retention', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      /* A purge cannot be undone, so it is two calls. The dry run deletes nothing
         and answers with what would go and whose it is — that is what the dialog
         shows, rather than a sentence the browser made up. The real call has to
         carry the store's exact name (the API refuses an empty body with 422),
         and the operator types it, so the name is a decision and not a default. */
      async function purgeStore(r){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Purging a store requires the operator role.');
          return;
        }
        let preview;
        try { preview = await API.memory.purge(r.id, { dry_run: true }); }
        catch (err) {
          // 412 is the store saying why it cannot be purged: no retention policy,
          // paused, or no agent names it as its memory policy. Its message says which.
          toast(err.status === 412 ? 'warn' : 'error', 'Cannot purge', err.message);
          return;
        }
        const count = preview.candidate_records || 0;
        if(!count && !preview.capped){
          toast('info','Nothing to purge', `No record of “${r.name}” is past its ${preview.retention_days}-day retention.`);
          return;
        }
        const agents = (preview.agents || []).map(esc).join(', ') || '—';
        openModal({
          title:'Purge Data — ' + r.name, icon:'alert',
          body:`<p style="margin:0">Permanently delete <b>${fmtFull(count)}</b> expired conversation record(s) held by: ${agents}.
              Runs outside a conversation are not touched.</p>
            <div class="small muted" style="margin-top:8px">In scope: records last active before ${esc(fmtDateTime(ts(preview.cutoff)))}
              (${esc(String(preview.retention_days))}-day retention)${preview.capped ? '. The count stopped at the limit, so there may be more' : ''}.
              A backup does not hold records, so this cannot be undone. It is written to the audit trail.</div>
            <label class="auth-field" style="margin-top:12px"><span>Type the store's name to confirm</span>
              <input type="text" id="pgConfirm" autocomplete="off" placeholder="${esc(r.name)}"></label>`,
          footer:[
            {label:'Cancel'},
            {label:'Purge', cls:'danger', onClick: async (close, modal) => {
              if(modal.querySelector('#pgConfirm').value.trim() !== r.name){
                toast('error','Name does not match', `Type “${r.name}” exactly to purge it.`);
                return;
              }
              close();
              try {
                const res = await API.memory.purge(r.id, { confirm: r.name });
                // Partial or capped: what was deleted is deleted, and more is waiting.
                const again = res && (res.partial || res.capped);
                toast(again ? 'warn' : 'success', again ? 'Purge incomplete — run it again' : 'Purge complete',
                  res && res.message ? res.message : `${fmtFull(res ? res.purged_records : null)} record(s) removed.`);
                if(table) table.refresh();
                loadKpis();
              } catch (err) {
                // 409: another purge of this store is still running; the message says so.
                toast(err.status === 409 ? 'warn' : 'error', 'Could not purge', err.message);
              }
            }},
          ],
        });
      }

      function createBackup(r){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Creating a backup requires the operator role.');
          return;
        }
        /* Said plainly, because the old copy promised the opposite: a backup is a
           snapshot of how the store is governed. No conversation is copied
           anywhere, so nobody should run a purge believing this can undo it. */
        openModal({
          title:'Create Backup — ' + r.name, icon:'history',
          body:`<div class="small muted">A backup is a snapshot of the store's governance: its retention policy and its status, with a count of the conversation threads its agents hold right now.
            <b>Threads are counted, not copied</b> — a purge cannot be undone from a backup. Restore puts the policy and status back.</div>`,
          footer:[
            {label:'Create Backup', cls:'primary', onClick: async (close) => {
              close();
              try {
                const res = await API.memory.backup(r.id);
                toast('success','Snapshot saved', res && res.notice ? res.notice
                  : res && res.record_count != null ? `${fmtNum(res.record_count)} thread(s) counted, none copied.` : 'Retention policy and status captured.');
                // A snapshot moves no count, so the KPI row — a read of the telemetry store — is left alone.
                if(table) table.refresh();
              } catch (err) { toast('error','Could not create backup', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      function restoreBackup(r){
        if(!Store.session.can('admin')){
          toast('error','Not permitted','Restoring from a backup requires the admin role.');
          return;
        }
        openModal({
          title:'Restore — ' + r.name, icon:'refresh',
          body:`<div class="card-loading" style="height:120px"></div>`,
          footer:[{label:'Cancel'}],
          onOpen(modal){
            const host = modal.querySelector('.modal-body');
            API.memory.storeBackups(r.id)
              .then(page => {
                const rows = page.items || page;
                if(!rows.length){
                  host.innerHTML = `<div class="empty-state">${ICONS.history}<div class="es-title">No backups for this store</div><div>Create one first.</div></div>`;
                  return;
                }
                host.innerHTML = `<label class="auth-field"><span>Backup to restore</span>
                  <select class="filter-select" id="rbPick" style="height:34px">${rows.map(b=>
                    `<option value="${esc(b.id)}">${esc(fmtDateTime(ts(b.created_at)))} — ${fmtNum(b.record_count)} threads counted (${esc(b.kind)})</option>`).join('')}</select></label>
                  <div class="small muted" style="margin-top:10px">Restoring puts back the retention policy and status only. Conversation records are never restored: a backup does not hold them.</div>`;
                const foot = modal.querySelector('.modal-foot');
                const btn = document.createElement('button');
                btn.className = 'btn primary';
                btn.textContent = 'Restore';
                btn.addEventListener('click', async () => {
                  const backupId = modal.querySelector('#rbPick').value;
                  modal.querySelector('[data-mclose]').click();
                  try {
                    const res = await API.memory.restore(r.id, { backup_id: backupId });
                    // What actually came back, in the server's words — which may be nothing,
                    // when the store already matched the snapshot.
                    const fields = ((res && res.fields_restored) || []).map(f => f.replace(/_/g, ' '));
                    toast('success','Restore complete', [
                      fields.length ? `Restored: ${fields.join(', ')}.` : 'Nothing differed from the snapshot.',
                      res && res.notice ? res.notice : '',
                    ].filter(Boolean).join(' '), 6000);
                    // No count moves on a restore, so the KPI row — a read of the telemetry
                    // store — is re-asked only when a status came back, which its sub-line shows.
                    if(table) table.refresh();
                    if(fields.includes('status')) loadKpis();
                  } catch (err) { toast('error','Could not restore', err.message); }
                });
                foot.insertBefore(btn, foot.firstChild);
              })
              .catch(err => { host.innerHTML=''; host.appendChild(screenError(err, null, 'the backup list')); });
          },
        });
      }

      function createStore(){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Creating a memory store requires the operator role.');
          return;
        }
        openModal({
          title:'Create Memory Store', icon:'database',
          body:`<label class="auth-field"><span>Name</span><input type="text" id="csName" placeholder="Support conversation memory"></label>
            <div class="grid g2" style="margin-top:10px">
              <label class="auth-field"><span>Type</span><select class="filter-select" id="csType" style="height:34px">
                ${['Conversation','Vector','Key-Value','Session','Long-term'].map(o=>`<option>${o}</option>`).join('')}</select></label>
              <label class="auth-field"><span>Environment</span><select class="filter-select" id="csEnv" style="height:34px">
                ${['Production','Staging','Development'].map(o=>`<option>${o}</option>`).join('')}</select></label>
            </div>
            <label class="auth-field" style="margin-top:10px"><span>Retention (days)</span>
              <input type="number" id="csDays" placeholder="Defaults to 90" min="1"></label>`,
          footer:[
            {label:'Create Store', cls:'primary', onClick: async (close, modal) => {
              const days = modal.querySelector('#csDays').value;
              const payload = {
                name: modal.querySelector('#csName').value.trim(),
                store_type: modal.querySelector('#csType').value,
                environment: modal.querySelector('#csEnv').value,
              };
              // Blank means the server's 90-day default, not "indefinite" — the API has no indefinite.
              if(days !== '') payload.retention_days = Number(days);
              close();
              try {
                await API.memory.create(payload);
                toast('success','Memory store created', `${payload.name} is ready.`);
                renderTab(0);
                loadKpis();
              } catch (err) { toast('error','Could not create store', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      // tabBar only reports clicks; the first tab has to be asked for.
      renderTab(0);
    },
  };

  /* ================= DEPLOYMENT & ENVIRONMENT ================= */
  SCREENS['deployments'] = {
    title:'Environments & Releases',
    render(main){
      main.innerHTML = `
        ${pageHead({title:'Environments & Releases', sub:'Environments, releases and rollouts for every governed agent.',
          actions:`${searchBox('dpSearch','Search deployments…')}
          <button class="btn" id="dpExport">${ICONS.download}Export</button>
          <button class="btn primary" id="dpCreate">${ICONS.plus}Create Deployment</button>`})}
        <div id="dpKpis">${kpiSkeleton(['Total Environments','Active Deployments','Successful','Failed','Avg. Deployment Time','Rollbacks'])}</div>
        <div id="dpTabs"></div>
        <div id="dpBody"></div>`;

      const body = document.getElementById('dpBody');
      let table = null, liveStream = null;

      loadKpis();
      function loadKpis(){
        const host = document.getElementById('dpKpis');
        if(!host) return;
        API.deployments.summary()
          .then(s => {
            if(!host.isConnected) return;
            host.innerHTML = kpiRow([
              {label:'Total Environments', value:fmtFull(s.total_environments), icon:'layers', color:'purple'},
              {label:'Active Deployments', value:fmtFull(s.active_deployments), icon:'rocket', color:'green',
               sub:`${fmtFull(s.total_deployments)} all time`},
              {label:'Successful', value:pct(s.success_rate_percent), icon:'checkCircle', color:'blue',
               sub:`${fmtFull(s.successful_deployments)} of ${fmtFull(s.total_deployments)}`},
              {label:'Failed', value:fmtFull(s.failed_deployments), icon:'xCircle', color:'red',
               sub:`${fmtFull(s.halted_deployments)} halted`},
              {label:'Avg. Deployment Time', value:s.avg_deployment_time || '—', icon:'clock', color:'amber'},
              {label:'Rollbacks', value:fmtFull(s.rollbacks_executed), icon:'refresh', color:'orange',
               sub:`${fmtFull(s.rolled_back_deployments)} deployment(s) rolled back`},
            ]);
          })
          .catch(err => { host.innerHTML=''; host.appendChild(screenError(err, loadKpis, 'the deployment summary')); });
      }

      tabBar(document.getElementById('dpTabs'),
        [{label:'Environments'},{label:'Deployments'},{label:'Approvals'},{label:'History'}],
        renderTab);

      document.getElementById('dpSearch').addEventListener('input', e => { if(table) table.search(e.target.value); });
      document.getElementById('dpExport').addEventListener('click', async () => {
        /* The file is always the deployments list. Only the two deployment tabs
           hold a query that means anything to it: from Environments or Approvals
           the table's sort key ("name", "request_ref") is one the export refuses
           with 422, and its search text was typed about something else. */
        const onDeployments = currentTab === 1 || currentTab === 3;
        try { await API.deployments.export(onDeployments && table ? table.params() : {}); toast('success','Export complete','Deployments exported to CSV.'); }
        catch (err) { toast('error','Export failed', err.message); }
      });
      document.getElementById('dpCreate').addEventListener('click', createDeployment);

      let currentTab = 0;
      function renderTab(i){
        currentTab = i;
        body.innerHTML = '';
        table = null;
        if(i === 0) return environmentsTab();
        if(i === 1) return deploymentsTab({});
        if(i === 2) return approvalsTab();
        return deploymentsTab({ terminal: true });
      }

      function environmentsTab(){
        /* Nothing seeds an environment, so without this button a new workspace
           had an empty table, a Create Deployment that answered "No
           environments", and nowhere to register one short of curl. */
        body.innerHTML = `<div class="flex" style="justify-content:flex-end;gap:8px;margin-bottom:12px">
            <button class="btn primary" id="dpAddEnv">${ICONS.plus}Add Environment</button>
          </div><div id="dpEnvTable"></div>`;
        table = dataTable({
          columns:[
            { key:'name', label:'Environment', render:r=>entityCell(r.name, r.description || r.env_type, 'layers',
                r.env_type==='Production'?'green':r.env_type==='Staging'?'cyan':'purple') },
            { key:'env_type', label:'Type', render:r=>badge(r.env_type) },
            { key:'region', label:'Region', render:r=>r.region?esc(r.region):dash },
            { key:'status', label:'Status', render:r=>statusText(r.status) },
            // Releases the environment is serving now, not every success it ever had.
            { key:'active_deployment_count', label:'Live Releases', align:'right', cls:'num', render:r=>fmtFull(r.active_deployment_count) },
            { key:'health', label:'Health', render:r=>r.health==null?dash:barPct(r.health, r.health>=90?'green':r.health>=70?'amber':'red') },
            // Joined in from the deployment history; the server cannot order by it.
            { key:'last_deployment_at', label:'Last Deployment', sortable:false, render:r=>`<span class="dim nowrap">${when(r.last_deployment_at)}</span>` },
          ],
          rowId:'id', itemName:'environments', pageSize:10,
          emptyText:'No environments registered yet — add one to deploy into',
          source: (p) => API.environments.list(p),
          rowActions: r => [
            {label:'Restart Services', icon:'refresh', onClick:()=>restartEnvironment(r)},
            {label:'Environment Settings', icon:'settings', onClick:()=>environmentSettings(r)},
            {sep:true},
            {label:'Delete Environment', icon:'trash', danger:true, onClick:()=>deleteEnvironment(r)},
          ],
        });
        document.getElementById('dpEnvTable').appendChild(table.el);
        document.getElementById('dpAddEnv').addEventListener('click', addEnvironment);
      }

      const ENV_TYPES = ['Production','Staging','UAT','Development','QA','Sandbox','DR'];
      const ENV_STATUSES = ['Healthy','Degraded','Standby','Offline'];

      /** The fields an environment is made of. Adding one and editing one share them. */
      function environmentForm(r){
        r = r || {};
        const options = (list, picked) => list.map(o=>`<option ${o===picked?'selected':''}>${o}</option>`).join('');
        return `<label class="auth-field"><span>Name</span><input type="text" id="enName" maxlength="80" value="${esc(r.name||'')}" placeholder="e.g. Production EU"></label>
          <div class="grid g2" style="margin-top:10px">
            <label class="auth-field"><span>Type</span><select class="filter-select" id="enType" style="height:34px">${options(ENV_TYPES, r.env_type)}</select></label>
            <label class="auth-field"><span>Status</span><select class="filter-select" id="enStatus" style="height:34px">${options(ENV_STATUSES, r.status || 'Healthy')}</select></label>
          </div>
          <div class="grid g2" style="margin-top:10px">
            <label class="auth-field"><span>Region</span><input type="text" id="enRegion" maxlength="60" value="${esc(r.region||'')}" placeholder="e.g. eu-west-1"></label>
            <label class="auth-field"><span>Reported health (%)</span><input type="number" id="enHealth" min="0" max="100" step="0.1" value="${r.health==null?'':esc(String(r.health))}" placeholder="Not reported"></label>
          </div>
          <label class="auth-field" style="margin-top:10px"><span>Description</span><input type="text" id="enDesc" maxlength="2000" value="${esc(r.description||'')}"></label>
          <div class="small muted" style="margin-top:8px">Health is the uptime figure your monitoring reports for this environment. Nothing probes it for you: left blank, the Health column shows a dash rather than a guess.</div>`;
      }

      /** The form as a request body, or null (after saying why) when it cannot be sent. */
      function readEnvironmentForm(modal){
        const name = modal.querySelector('#enName').value.trim();
        if(!name){ toast('error','Name required','Give the environment a name.'); return null; }
        const reported = modal.querySelector('#enHealth').value.trim();
        const health = reported === '' ? null : Number(reported);
        if(health != null && !(health >= 0 && health <= 100)){
          toast('error','Health out of range','Reported health is a percentage between 0 and 100.');
          return null;
        }
        return {
          name,
          env_type: modal.querySelector('#enType').value,
          status: modal.querySelector('#enStatus').value,
          region: modal.querySelector('#enRegion').value.trim() || null,
          health,
          description: modal.querySelector('#enDesc').value.trim() || null,
        };
      }

      function addEnvironment(){
        if(!Store.session.can('admin')){
          toast('error','Not permitted','Adding an environment requires the admin role.');
          return;
        }
        openModal({
          title:'Add Environment', icon:'layers',
          body: environmentForm(),
          footer:[
            {label:'Add Environment', cls:'primary', onClick: async (close, modal) => {
              const payload = readEnvironmentForm(modal);
              if(!payload) return;
              try {
                // Left open on failure: a 409 is a name already taken, which is fixed in this form.
                const created = await API.environments.create(payload);
                close();
                toast('success','Environment added', `${created.name} can now be deployed into.`);
                if(table) table.refresh();
                loadKpis();
              } catch (err) { toast('error','Could not add environment', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      function deleteEnvironment(r){
        if(!Store.session.can('admin')){
          toast('error','Not permitted','Deleting an environment requires the admin role.');
          return;
        }
        confirmModal({
          title:'Delete Environment', confirmLabel:'Delete', danger:true,
          msg:`Delete “${r.name}”? Its deployment history stays, but nothing can be released into it again.`,
          onConfirm: async () => {
            try {
              await API.environments.remove(r.id);
              toast('success','Environment deleted', r.name + ' removed.');
              if(table) table.refresh();
              loadKpis();
            } catch (err) {
              // 409 names what is in the way: releases in flight, or live ones — for
              // those the message says to take the environment Offline first.
              toast('error','Could not delete environment', err.message);
            }
          },
        });
      }

      function restartEnvironment(r){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Restarting services requires the operator role.');
          return;
        }
        confirmModal({
          title:'Restart Services', confirmLabel:'Restart', danger:true,
          msg:`Restart every service in “${r.name}”? In-flight requests to agents in this environment will fail.`,
          onConfirm: async () => {
            try {
              const res = await API.environments.restart(r.id);
              toast('success','Restart requested', res && res.message ? res.message : `${r.name} is restarting.`);
              if(table) table.refresh();
            } catch (err) { toast('error','Could not restart', err.message); }
          },
        });
      }

      function environmentSettings(r){
        if(!Store.session.can('admin')){
          toast('error','Not permitted','Environment settings require the admin role.');
          return;
        }
        openModal({
          title:'Environment Settings — ' + r.name, icon:'settings',
          body: environmentForm(r),
          footer:[
            {label:'Save', cls:'primary', onClick: async (close, modal) => {
              const payload = readEnvironmentForm(modal);
              if(!payload) return;
              try {
                // The server writes only what differs, so sending the whole form is safe.
                const saved = await API.environments.update(r.id, payload);
                close();
                toast('success','Environment updated', saved.name + ' saved.');
                if(table) table.refresh();
                loadKpis();
              } catch (err) { toast('error','Could not update environment', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      function deploymentsTab(extra){
        body.innerHTML = `<div class="with-inspector" id="dpLayout"><div id="dpTableWrap"></div><div class="inspector" id="dpInspector"></div></div>`;
        table = dataTable({
          columns:[
            { key:'deployment_ref', label:'Deployment', render:r=>entityCell(r.deployment_ref, r.agent_name || '—', 'rocket', 'purple') },
            { key:'version', label:'Version', render:r=>`<span class="mono">${esc(r.version)}</span>` },
            // A label joined in by the server, which can order by the id behind it but not by the name.
            { key:'environment_name', label:'Environment', sortable:false, render:r=>r.environment_name?badge(r.environment_name):dash },
            { key:'strategy', label:'Strategy', render:r=>badge(r.strategy) },
            { key:'status', label:'Status', render:r=>statusText(r.status) },
            { key:'health', label:'Health', render:r=>r.health==null?dash:barPct(r.health, r.health>=90?'green':'amber') },
            // Keyed by the seconds the server orders by; the label is only how they read.
            { key:'duration_seconds', label:'Duration', align:'right', cls:'num', render:r=>r.duration_label?esc(r.duration_label):dash },
            { key:'started_at', label:'Started', render:r=>`<span class="dim nowrap">${when(r.started_at)}</span>` },
          ],
          rowId:'id', itemName:'deployments', pageSize:12,
          emptyText: extra.terminal ? 'No completed deployments yet' : 'No deployments yet',
          extraParams: extra.terminal ? { terminal: true } : null,
          filters:[
            {key:'status', label:'Status', param:'status', options:['Queued','Running','Succeeded','Failed','Halted','RolledBack'], allLabel:'All Statuses'},
            {key:'strategy', label:'Strategy', param:'strategy', options:['Rolling','Blue-Green','Canary','Recreate'], allLabel:'All Strategies'},
          ],
          source: (p) => API.deployments.list(p),
          exportSource: (p) => API.deployments.export(p),
          autoSelectFirst: true,
          onSelect: showDeployment,
          rowActions: r => {
            const actions = [];
            if(!r.is_terminal){
              actions.push({label:'Watch Pipeline', icon:'activity', onClick:()=>watchDeployment(r)});
              /* Four eyes: the server refuses an approval from whoever started the
                 release, so that person is not offered one — they can still halt
                 it. Only a release that has raised its gate has anything to approve. */
              const mine = r.triggered_by_user_id && r.triggered_by_user_id === (Store.session.user || {}).id;
              if(r.approval_request_id && !mine) actions.push({label:'Approve', icon:'stamp', onClick:()=>decideGate(r)});
              actions.push({label:'Halt Deployment', icon:'pause', danger:true, onClick:()=>halt(r)});
            }
            if(r.status === 'Succeeded'){
              actions.push({label:'Promote', icon:'trendUp', onClick:()=>promote(r)});
              // Only a release that is being served can be undone. One that failed or
              // was halted never went live, and a rolled-back one is already undone.
              actions.push({label:'Roll Back', icon:'refresh', danger:true, onClick:()=>rollBack(r)});
            }
            return actions;
          },
        });
        const wrap = document.getElementById('dpTableWrap');
        wrap.appendChild(table.filterEl);
        wrap.appendChild(table.el);
      }

      async function halt(r){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Halting a deployment requires the operator role.');
          return;
        }
        try {
          const res = await API.deployments.halt(r.id, {});
          toast('success','Halted', res && res.message ? res.message : `${r.deployment_ref} halted.`);
          if(table) table.refresh();
          loadKpis();
        } catch (err) { toast('error','Could not halt', err.message); }
      }

      /** Decide the approval gate. It closes a gate on someone else's release, so it takes its own role. */
      function decideGate(r){
        if(!Store.session.can('approver')){
          toast('error','Not permitted','Approving a release requires the approver role.');
          return;
        }
        const decide = (approved) => async (close, modal) => {
          const note = modal.querySelector('#dgNote').value.trim() || null;
          close();
          try {
            const res = await API.deployments.approve(r.id, { approved, note });
            toast('success', approved ? 'Approved' : 'Rejected',
              res && res.message ? res.message : `${r.deployment_ref} ${approved ? 'approved' : 'rejected'}.`);
          } catch (err) {
            // 403 says who has to approve instead; 409 that the request was already
            // decided in Approvals & Audit, and what to do about it. Both read as written.
            toast('error', approved ? 'Could not approve' : 'Could not reject', err.message);
          }
          // Either way the row may have moved: the other screen can decide the same gate.
          if(table) table.refresh();
          loadKpis();
          Store.refreshBadges();
        };
        openModal({
          title:'Approve ' + r.deployment_ref, icon:'stamp',
          body:`<div class="small muted" style="margin-bottom:10px">${esc(r.version)} → ${esc(r.environment_name || 'environment')}. Approving lets the pipeline continue to Deploy; rejecting halts the release and frees the environment.</div>
            <label class="auth-field"><span>Note</span><input type="text" id="dgNote" maxlength="1000" placeholder="Recorded with the decision (optional)"></label>`,
          footer:[
            {label:'Approve', cls:'primary', onClick: decide(true)},
            {label:'Reject', cls:'danger', onClick: decide(false)},
            {label:'Cancel'},
          ],
        });
      }

      function rollBack(r){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Rolling back a deployment requires the operator role.');
          return;
        }
        openModal({
          title:'Roll Back ' + r.deployment_ref, icon:'refresh',
          body:`<div class="small muted" style="margin-bottom:10px">A rollback is a new deployment with its own pipeline and gate. ${esc(r.deployment_ref)} keeps serving — and keeps reading Succeeded — until that rollback lands.</div>
            <label class="auth-field"><span>Version to go back to</span><input type="text" id="rbVersion" maxlength="40" placeholder="Blank: the release that was live before ${esc(r.version)}"></label>
            <label class="auth-field" style="margin-top:10px"><span>Reason</span><input type="text" id="rbReason" maxlength="1000" placeholder="Why is this release being undone?"></label>`,
          footer:[
            {label:'Roll Back', cls:'danger', onClick: async (close, modal) => {
              const payload = {
                target_version: modal.querySelector('#rbVersion').value.trim() || null,
                reason: modal.querySelector('#rbReason').value.trim() || null,
              };
              try {
                const res = await API.deployments.rollback(r.id, payload);
                close();
                const ref = res && res.data && res.data.deployment_ref;
                toast('success', ref ? `Rollback queued as ${ref}` : 'Rollback queued',
                  res && res.message ? res.message : `${r.deployment_ref} stays live until the rollback lands.`);
                if(table) table.refresh();
                loadKpis();
              } catch (err) {
                // Left open: a 412 means there is no earlier release here to fall back
                // to, so the version has to be named in this form.
                toast('error','Could not roll back', err.message);
                if(err.status === 412){ const v = modal.querySelector('#rbVersion'); if(v) v.focus(); }
              }
            }},
            {label:'Cancel'},
          ],
        });
      }

      async function promote(r){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Promoting a deployment requires the operator role.');
          return;
        }
        let envs;
        try { envs = await API.environments.list({ page_size: 50 }); }
        catch (err) { toast('error','Could not load environments', err.message); return; }
        openModal({
          title:'Promote ' + r.deployment_ref, icon:'trendUp',
          body:`<label class="auth-field"><span>Target environment</span>
            <select class="filter-select" id="prEnv" style="height:34px">${envs.items
              .filter(e => e.id !== r.environment_id)
              .map(e=>`<option value="${esc(e.id)}">${esc(e.name)} (${esc(e.env_type)})</option>`).join('')}</select></label>`,
          footer:[
            {label:'Promote', cls:'primary', onClick: async (close, modal) => {
              const envId = modal.querySelector('#prEnv').value;
              close();
              try {
                await API.deployments.promote(r.id, { target_environment_id: envId });
                toast('success','Promoted', `${r.deployment_ref} promoted.`);
                if(table) table.refresh();
                loadKpis();
              } catch (err) { toast('error','Could not promote', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      function showDeployment(r){
        const insp = document.getElementById('dpInspector');
        if(!insp || !r) return;
        document.getElementById('dpLayout').classList.remove('collapsed');
        insp.innerHTML = `
          <div class="insp-head"><div><div class="insp-title">${esc(r.deployment_ref)}</div>
            <div class="insp-sub mono">${esc(r.version)}</div></div>
            <button class="icon-btn insp-close" id="dpInspClose">${ICONS.x}</button></div>
          ${inspSection('Release','rocket', kv([
            ['Agent', r.agent_id?`<span class="link" data-nav="agent/${esc(r.agent_id)}">${esc(r.agent_name)}</span>`:esc(r.agent_name||'—')],
            ['Environment', r.environment_name?esc(r.environment_name):dash],
            ['Strategy', esc(r.strategy)],
            ['Status', statusText(r.status)],
            ['Health', r.health==null?dash:pct(r.health,0)],
            ['Commit', r.commit_ref?`<span class="mono">${esc(r.commit_ref)}</span>`:dash],
          ]))}
          ${inspSection('Timing','clock', kv([
            ['Started', when(r.started_at)],
            ['Finished', when(r.finished_at)],
            ['Duration', r.duration_label?esc(r.duration_label):dash],
          ]))}
          ${r.notes ? inspSection('Notes','chat', `<div class="quote">${esc(r.notes)}</div>`) : ''}
          <div id="dpStages"></div>`;
        insp.querySelector('#dpInspClose').addEventListener('click', ()=>document.getElementById('dpLayout').classList.add('collapsed'));

        section(insp.querySelector('#dpStages'), () => API.deployments.stages(r.id), stages => {
          const rows = stages.items || stages;
          if(!rows.length) return card('Pipeline', `<div class="empty-state">${ICONS.rocket}<div class="es-title">No stages recorded</div></div>`);
          return card('Pipeline', `<div class="pipe">${rows.map(s=>stageHtml(s)).join('')}</div>`);
        }, 'the pipeline');
      }

      function stageHtml(s){
        // Approved closes an approval gate and Warning passes while flagging, so both are finished.
        const finished = s.status === 'Completed' || s.status === 'Approved' || s.status === 'Warning';
        const state = s.status === 'Failed' ? 'fail' : s.status === 'Skipped' ? 'skip' : finished ? 'done' : 'run';
        const dur = s.started_at && s.finished_at
          ? Math.round((new Date(s.finished_at) - new Date(s.started_at)) / 1000) + 's' : '';
        return `<div class="pipe-step">
          <div class="pipe-dot ${state}">${state==='fail'?ICONS.x:state==='done'?ICONS.check:state==='skip'?ICONS.chevRight:ICONS.clock}</div>
          <div class="pipe-body"><div class="pipe-title"><span>${esc(s.name)}</span><span class="faint num">${dur}</span></div>
          <div class="pipe-sub">${esc(s.log || s.status)}</div></div></div>`;
      }

      function approvalsTab(){
        table = dataTable({
          columns:[
            // Keyed by what the approvals list orders by: the reference people quote
            // (REQ-1042), not a slice of the row id, and the time the request was raised.
            { key:'request_ref', label:'Request', render:r=>`<span class="mono">${esc(r.request_ref || String(r.id).slice(0,10)+'…')}</span>` },
            { key:'resource', label:'Resource', render:r=>esc(r.resource||'—') },
            { key:'requested_by_name', label:'Requested By', sortable:false, render:r=>r.requested_by_name?esc(r.requested_by_name):dash },
            { key:'risk', label:'Risk', render:r=>r.risk?C.riskBadge(r.risk):dash },
            { key:'status', label:'Status', render:r=>statusText(r.status) },
            { key:'requested_at', label:'Raised', render:r=>`<span class="dim nowrap">${when(r.requested_at)}</span>` },
          ],
          rowId:'id', itemName:'approval requests', pageSize:10,
          emptyText:'No deployment approvals raised',
          source: (p) => API.deployments.approvals(p),
          rowActions: r => {
            /* Deciding here and deciding in Approvals & Audit are the same write —
               both close the request and move the release's gate — so the decision
               is offered on the row rather than only as a link away from the screen.
               It is offered only where the server would take it: the request still
               open, a release named in its payload, and not one this person started
               (four eyes — the starter is answered 403 in either screen). */
            const deploymentId = (r.payload || {}).deployment_id;
            const mine = r.requested_by_user_id && r.requested_by_user_id === (Store.session.user || {}).id;
            const actions = [];
            if(r.is_open && deploymentId && !mine){
              actions.push({label:'Approve', icon:'checkCircle', onClick:()=>decideRequest(r, true)});
              actions.push({label:'Reject', icon:'xCircle', danger:true, onClick:()=>decideRequest(r, false)});
            }
            actions.push({label:'Open in Approvals & Audit', icon:'stamp', onClick:()=>APP.go('approvals')});
            return actions;
          },
        });
        body.appendChild(table.el);
      }

      /** Decide a queued gate from the Approvals tab, on the release its payload names. */
      function decideRequest(r, approved){
        if(!Store.session.can('approver')){
          toast('error','Not permitted', `${approved ? 'Approving' : 'Rejecting'} a release requires the approver role.`);
          return;
        }
        const ref = (r.payload || {}).deployment_ref || r.request_ref;
        openModal({
          title:`${approved ? 'Approve' : 'Reject'} ${r.request_ref}`, icon: approved ? 'checkCircle' : 'xCircle',
          body:`<div class="small muted" style="margin-bottom:10px">${esc(r.action_detail || r.action)}${r.resource ? ' — ' + esc(r.resource) : ''}. ${approved
              ? `Approving lets ${esc(ref)} continue to Deploy.`
              : `Rejecting halts ${esc(ref)} and frees the environment.`}</div>
            <label class="auth-field"><span>${approved ? 'Note' : 'Reason (required)'}</span>
              <input type="text" id="daNote" maxlength="1000" placeholder="${approved ? 'Recorded with the decision (optional)' : 'Why is this being refused?'}"></label>`,
          footer:[
            {label: approved ? 'Approve' : 'Reject', cls: approved ? 'primary' : 'danger', onClick: async (close, modal) => {
              const note = modal.querySelector('#daNote').value.trim();
              // A refusal is quoted verbatim in the audit trail, so it is never blank.
              if(!approved && !note){
                toast('error','A reason is required','A rejection is recorded with the reason it was refused.');
                return;
              }
              close();
              try {
                // Through Store.mutate: the queue this row came from lives on another
                // screen, and the sidebar's Approvals badge has just gone down by one.
                const res = await Store.mutate(
                  () => API.deployments.approve(r.payload.deployment_id, { approved, note: note || null }),
                  { event:'approvals:changed' });
                toast('success', approved ? 'Approved' : 'Rejected',
                  res && res.message ? res.message : `${ref} ${approved ? 'approved' : 'rejected'}.`);
              } catch (err) {
                // 403 is the four-eyes refusal; 409 that it was already decided in
                // Approvals & Audit. Both say what happened and read as written.
                toast('error', approved ? 'Could not approve' : 'Could not reject', err.message);
              }
              // Either way the row has moved, or someone else moved it.
              if(table) table.refresh();
              loadKpis();
            }},
            {label:'Cancel'},
          ],
        });
      }

      async function createDeployment(){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Creating a deployment requires the operator role.');
          return;
        }
        let envs, agents;
        try {
          [envs, agents] = await Promise.all([
            API.environments.list({ page_size: 50 }),
            API.deployments.agents({ page_size: 50 }),
          ]);
        } catch (err) { toast('error','Could not load targets', err.message); return; }

        if(!envs.items.length){ toast('warn','No environments','Add one on the Environments tab first — a release needs somewhere to go.'); return; }
        if(!agents.items.length){ toast('warn','No agents','Register an agent before deploying.'); return; }

        openModal({
          title:'Create Deployment', icon:'rocket',
          body:`<div class="grid g2">
              <label class="auth-field"><span>Agent</span><select class="filter-select" id="cdAgent" style="height:34px">
                ${agents.items.map(a=>`<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('')}</select></label>
              <label class="auth-field"><span>Environment</span><select class="filter-select" id="cdEnv" style="height:34px">
                ${envs.items.map(e=>`<option value="${esc(e.id)}">${esc(e.name)} (${esc(e.env_type)})</option>`).join('')}</select></label>
            </div>
            <div class="grid g2" style="margin-top:10px">
              <label class="auth-field"><span>Version</span><input type="text" id="cdVersion" placeholder="v1.4.0"></label>
              <label class="auth-field"><span>Strategy</span><select class="filter-select" id="cdStrategy" style="height:34px">
                ${['Rolling','Blue-Green','Canary','Recreate'].map(o=>`<option>${o}</option>`).join('')}</select></label>
            </div>
            <label class="auth-field" style="margin-top:10px"><span>Notes</span><input type="text" id="cdNotes" placeholder="What is in this release?"></label>`,
          footer:[
            {label:'Deploy', cls:'primary', onClick: async (close, modal) => {
              const version = modal.querySelector('#cdVersion').value.trim();
              if(!version){ toast('error','Version required','Give the release a version, e.g. v1.4.0.'); return; }
              const payload = {
                agent_id: modal.querySelector('#cdAgent').value,
                environment_id: modal.querySelector('#cdEnv').value,
                version,
                strategy: modal.querySelector('#cdStrategy').value,
                notes: modal.querySelector('#cdNotes').value.trim() || null,
              };
              try {
                const created = await API.deployments.create(payload);
                close();
                toast('success','Deployment started', `${created.deployment_ref} is running.`);
                // Every pipeline parks at its approval gate, which is not a terminal
                // frame — so waiting for one left the new row off the table, with
                // nothing to approve, until the tab was switched.
                if(table) table.refresh();
                loadKpis();
                watchDeployment(created);
              } catch (err) { toast('error','Could not start deployment', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      /** Follow the server's pipeline. The stages are its, not ours. */
      function watchDeployment(deployment){
        const opened = openModal({
          title:`Deploying ${deployment.version} → ${deployment.environment_name || 'environment'}`, icon:'rocket',
          body:`<div id="dpGate"></div><div id="dpLive"><div class="card-loading" style="height:160px"></div></div>`,
          footer:[{label:'Close'}],
        });
        const host = opened.el.querySelector('#dpLive');
        const gate = opened.el.querySelector('#dpGate');

        function paint(stages){
          if(!host || !host.isConnected) return;
          host.innerHTML = stages.length
            ? `<div class="pipe">${stages.map(stageHtml).join('')}</div>`
            : `<div class="empty-state">${ICONS.rocket}<div class="es-title">Waiting for the first stage…</div></div>`;
        }

        if(liveStream) liveStream.close();
        let mine = null, lastStatus = deployment.status;
        // Only ever our own stream: a second watch opened since owns `liveStream` now.
        function stop(){
          if(!mine) return;
          mine.close();
          if(liveStream === mine) liveStream = null;
          mine = null;
        }

        // The stream's frames are unnamed: each is a full pipeline snapshot, and
        // the server closes after the terminal one — we must close our end too,
        // or the EventSource would reconnect against a finished deployment forever.
        mine = liveStream = API.deployments.stream(deployment.id, {
          onMessage: (frame) => {
            if(!frame) return;
            if(frame.stages) paint(frame.stages);
            const moved = frame.status !== lastStatus;
            lastStatus = frame.status;
            if(moved && !frame.awaiting_approval && !frame.is_terminal && table) table.refresh();
            if(frame.awaiting_approval){
              /* Parked at the gate, where a release can sit for hours waiting on a
                 person. Say so and hang up rather than hold a connection open on a
                 bar that will not move; Watch Pipeline picks it up again. The row is
                 re-read because it has only now raised the request Approve needs. */
              if(gate && gate.isConnected){
                gate.innerHTML = `<div class="scan-note" style="margin-bottom:12px">${ICONS.clock} Waiting for approval — decide it here from the row's menu, or in Approvals &amp; Audit. Whoever started the release cannot approve it.</div>`;
              }
              stop();
              if(table) table.refresh();
              return;
            }
            if(!frame.is_terminal) return;
            stop();
            if(table) table.refresh();
            loadKpis();
            const ok = frame.status === 'Succeeded';
            toast(ok ? 'success' : 'warn', 'Deployment finished',
              `${deployment.deployment_ref} ${ok ? 'completed' : 'finished with status ' + frame.status}.`);
          },
          onError: () => refreshStages(),
        });

        /* The dialog has four ways out — Close, the ×, Escape, a click outside —
           and none of them told the stream, which api.js then kept reconnecting
           for as long as the tab lived. openModal has no close hook, so watch for
           the dialog leaving the page instead; that covers all four. */
        const overlay = opened.el.parentElement;
        if(overlay && overlay.parentElement){
          const gone = new MutationObserver(() => {
            if(overlay.isConnected) return;
            gone.disconnect();
            stop();
          });
          gone.observe(overlay.parentElement, { childList: true });
        }

        function refreshStages(){
          API.deployments.stages(deployment.id)
            .then(s => paint(s.items || s))
            .catch(() => {});
        }
        refreshStages();
      }

      // tabBar only reports clicks; the first tab has to be asked for.
      renderTab(0);

      this.cleanup = () => { if(liveStream){ liveStream.close(); liveStream = null; } };
    },
  };
})();
