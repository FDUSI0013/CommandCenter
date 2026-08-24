/* Fulcrum Ops — OPERATIONS screens: Quota Cost & Capacity, Memory & State, Deployment & Environment
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

  /* ================= QUOTA, COST & CAPACITY ================= */
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

      loadKpis();
      function loadKpis(){
        const host = document.getElementById('qcKpis');
        if(!host) return;
        API.quota.summary()
          .then(s => {
            if(!host.isConnected) return;
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
        try { await API.quota.export({ q: searchTerm }); toast('success','Export complete','Quotas exported to CSV.'); }
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
          () => API.quota.usageSeries({ metric: 'cost' }),
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
          () => API.quota.costByService({ page_size: 8 }),
          page => {
            if(!page.items.length) return emptyCard('Cost by Service','No service costs recorded');
            const total = page.items.reduce((a,r)=>a + (r.cost_usd||0), 0);
            return card('Cost by Service',
              `<div class="donut-wrap">${donut({ segments: page.items.map(r=>({value:r.cost_usd||0, color:r.color})),
                size:140, thickness:16, centerVal: money(total), centerLabel:'Total Spend' })}
              <div class="legend grow">${page.items.map(r=>`<div class="legend-item">
                <span class="sw" style="background:${U.cc(r.color)}"></span>
                <span class="lg-label" style="font-size:11.5px">${esc(r.label)}</span>
                <span class="lg-val">${esc(r.cost_display)}</span>
                <span class="lg-pct">${r.share_percent==null?'—':Math.round(r.share_percent)+'%'}</span></div>`).join('')}</div></div>`);
          }, 'the cost breakdown');

        section(document.getElementById('qcQuotaMini'),
          () => API.quota.quotas.list({ page_size: 6, sort: '-utilization_percent' }),
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
          () => API.quota.teamAllocation({ page_size: 10 }),
          page => {
            if(!page.items.length) return emptyCard('Cost & Usage by Team','No team allocation yet — assign owners to agents to see this');
            return card('Cost & Usage by Team',
              `<table class="tbl"><thead><tr><th>Team</th><th>Agents</th><th>Spend</th><th>% of Total</th><th>Tokens</th><th>API Calls</th><th>Avg / 1K</th><th>Trend</th></tr></thead><tbody>
                ${page.items.map(t=>`<tr style="cursor:default">
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
          () => API.quota.insights({ page_size: 6 }),
          page => {
            if(!page.items.length) return emptyCard('Insights','Nothing needs attention right now');
            return card('Insights',
              `<div class="insight-grid">${page.items.map(x=>`
                <div class="insight ${esc(x.severity.toLowerCase())}">
                  <span class="insight-ico" style="color:var(--${esc(x.color)})">${ICONS[x.icon]||ICONS.info}</span>
                  <div><div class="insight-title">${esc(x.title)}</div>
                  <div class="insight-body">${esc(x.body)}</div>
                  ${x.potential_savings_usd ? `<div class="insight-save">Potential saving ${money(x.potential_savings_usd)}</div>` : ''}
                  ${(x.recommendations||[]).length ? `<ul class="insight-recs">${x.recommendations.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>` : ''}
                  </div></div>`).join('')}</div>`);
          }, 'insights');

        body.addEventListener('click', e => {
          const link = e.target.closest('[data-gotab]');
          if(link){
            const idx = Number(link.dataset.gotab);
            const tab = document.querySelectorAll('#qcTabs .tab')[idx];
            if(tab) tab.click(); else renderTab(idx);
          }
        });
      }

      // ---- Quotas -------------------------------------------------------
      function quotasTab(){
        const table = dataTable({
          columns:[
            { key:'name', label:'Quota', render:r=>entityCell(r.name, r.scope_ref || r.scope, 'gauge', 'purple') },
            { key:'resource', label:'Resource', render:r=>badge(r.resource) },
            { key:'scope', label:'Scope', render:r=>esc(r.scope) },
            { key:'used_value', label:'Used', align:'right', cls:'num', render:r=>esc(r.used_display) },
            { key:'limit_value', label:'Limit', align:'right', cls:'num', render:r=>esc(r.limit_display) },
            { key:'utilization_percent', label:'Utilization', render:r=>barPct(r.utilization_percent, healthColor(r.health)) },
            { key:'enforcement', label:'Enforcement', render:r=>badge(r.enforcement) },
            { key:'period', label:'Period', render:r=>esc(r.period) },
            { key:'resets_label', label:'Resets', render:r=>r.resets_label?esc(r.resets_label):dash },
            { key:'health', label:'Status', render:r=>statusText(r.health, healthColor(r.health)) },
          ],
          rowId:'id', itemName:'quotas', pageSize:10, emptyText:'No quotas defined yet',
          extraParams: searchTerm ? { q: searchTerm } : null,
          filters:[
            {key:'resource', label:'Resource', param:'resource', options:['Tokens','Requests','Cost','Concurrency','Storage'], allLabel:'All Resources'},
            {key:'scope', label:'Scope', param:'scope', options:['Workspace','Agent','Team','Environment'], allLabel:'All Scopes'},
            {key:'health', label:'Health', param:'health', options:['Healthy','Watch','Critical'], allLabel:'All'},
          ],
          source: (p) => API.quota.quotas.list(p),
          exportSource: (p) => API.quota.export(p),
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
                toast('success','Request submitted','It is waiting in Approvals & Audit.');
                Store.refreshBadges();
              } catch (err) { toast('error','Could not submit', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      function createQuota(){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Creating a quota requires the operator role.');
          return;
        }
        openModal({
          title:'Create Quota', icon:'gauge',
          body:`<label class="auth-field"><span>Name</span><input type="text" id="cqName" placeholder="Production token ceiling"></label>
            <div class="grid g2" style="margin-top:10px">
              <label class="auth-field"><span>Resource</span><select class="filter-select" id="cqResource" style="height:34px">
                ${['Tokens','Requests','Cost','Concurrency','Storage'].map(o=>`<option>${o}</option>`).join('')}</select></label>
              <label class="auth-field"><span>Scope</span><select class="filter-select" id="cqScope" style="height:34px">
                ${['Workspace','Agent','Team','Environment'].map(o=>`<option>${o}</option>`).join('')}</select></label>
            </div>
            <div class="grid g2" style="margin-top:10px">
              <label class="auth-field"><span>Limit</span><input type="number" id="cqLimit" value="1000000" min="1"></label>
              <label class="auth-field"><span>Period</span><select class="filter-select" id="cqPeriod" style="height:34px">
                ${['Daily','Weekly','Monthly'].map(o=>`<option>${o}</option>`).join('')}</select></label>
            </div>
            <label class="auth-field" style="margin-top:10px"><span>Enforcement</span><select class="filter-select" id="cqEnf" style="height:34px">
              ${['Warn','Throttle','Block'].map(o=>`<option>${o}</option>`).join('')}</select></label>`,
          footer:[
            {label:'Create Quota', cls:'primary', onClick: async (close, modal) => {
              const payload = {
                name: modal.querySelector('#cqName').value.trim(),
                resource: modal.querySelector('#cqResource').value,
                scope: modal.querySelector('#cqScope').value,
                limit_value: Number(modal.querySelector('#cqLimit').value),
                period: modal.querySelector('#cqPeriod').value,
                enforcement: modal.querySelector('#cqEnf').value,
              };
              close();
              try {
                await API.quota.quotas.create(payload);
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
        openModal({
          title:'Edit Quota — ' + r.name, icon:'pen',
          body:`<label class="auth-field"><span>Limit</span><input type="number" id="eqLimit" value="${r.limit_value}" min="1"></label>
            <label class="auth-field" style="margin-top:10px"><span>Enforcement</span><select class="filter-select" id="eqEnf" style="height:34px">
              ${['Warn','Throttle','Block'].map(o=>`<option ${o===r.enforcement?'selected':''}>${o}</option>`).join('')}</select></label>`,
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

        section(document.getElementById('qcCostBreak'),
          () => API.quota.costBreakdown({ page_size: 10 }),
          page => {
            const rows = page.items || page;
            if(!rows.length) return emptyCard('Cost by Model','No model costs recorded');
            return card('Cost by Model',
              `<table class="tbl"><thead><tr><th>Model</th><th>Cost</th><th>Share</th></tr></thead><tbody>
                ${rows.map(r=>`<tr style="cursor:default"><td class="cell-main">${esc(r.label||r.key)}</td>
                  <td class="num">${esc(r.cost_display||money(r.cost_usd))}</td>
                  <td style="min-width:120px">${r.share_percent==null?dash:barPct(r.share_percent,'orange')}</td></tr>`).join('')}</tbody></table>`);
          }, 'the cost breakdown');

        section(document.getElementById('qcDrivers'),
          () => API.quota.topDrivers({ page_size: 8 }),
          page => {
            if(!page.items.length) return emptyCard('Top Cost Drivers','Nothing to rank yet');
            return card('Top Cost Drivers',
              `<table class="tbl"><thead><tr><th>Driver</th><th>Cost</th><th>Share</th></tr></thead><tbody>
                ${page.items.map(r=>`<tr style="cursor:default"><td class="cell-main">${esc(r.label)}</td>
                  <td class="num">${esc(r.cost_display)}</td><td class="num dim">${esc(r.share_display)}</td></tr>`).join('')}</tbody></table>`);
          }, 'cost drivers');

        section(document.getElementById('qcCostService'),
          () => API.quota.costByService({ page_size: 20 }),
          page => {
            if(!page.items.length) return emptyCard('Cost by Service','No service costs recorded');
            return card('Cost by Service',
              `<table class="tbl"><thead><tr><th>Service</th><th>Models</th><th>Cost</th><th>Share</th></tr></thead><tbody>
                ${page.items.map(r=>`<tr style="cursor:default"><td class="cell-main">${esc(r.label)}</td>
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
        [['tokens','Tokens Over Time'],['cost','Cost Over Time']].forEach(([metric,title]) => {
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
            { key:'period', label:'Period', render:r=>esc(r.period) },
            { key:'spent_usd', label:'Spent', align:'right', cls:'num', render:r=>esc(r.spent_display) },
            { key:'amount_usd', label:'Budget', align:'right', cls:'num', render:r=>esc(r.amount_display) },
            { key:'utilization_percent', label:'Utilization', render:r=>barPct(r.utilization_percent, healthColor(r.health)) },
            { key:'projected_spend_usd', label:'Projected', align:'right', cls:'num', render:r=>r.projected_spend_usd==null?dash:`${money(r.projected_spend_usd)}${r.on_pace_to_breach?' <span class="st-red">over</span>':''}` },
            { key:'warn_threshold_percent', label:'Thresholds', render:r=>`<span class="faint">${r.warn_threshold_percent}% / ${r.hard_threshold_percent}%</span>` },
            { key:'resets_label', label:'Resets', render:r=>r.resets_label?esc(r.resets_label):dash },
            { key:'health', label:'Status', render:r=>statusText(r.health, healthColor(r.health)) },
          ],
          rowId:'id', itemName:'budgets', pageSize:10, emptyText:'No budgets set',
          extraParams: searchTerm ? { q: searchTerm } : null,
          source: (p) => API.quota.budgets.list(p),
          rowActions: r => [
            {label:'Edit Thresholds', icon:'settings', onClick:()=>editThresholds(r)},
            {sep:true},
            {label:'Delete Budget', icon:'trash', danger:true, onClick:()=>confirmModal({
              title:'Delete Budget', confirmLabel:'Delete', danger:true,
              msg:`Delete “${r.name}”? Spend against it stops being tracked.`,
              onConfirm: async () => {
                try { await API.quota.budgets.remove(r.id); toast('success','Budget deleted', r.name+' removed.'); table.refresh(); }
                catch (err) { toast('error','Could not delete budget', err.message); }
              } })},
          ],
        });
        document.getElementById('qcBudgetTable').appendChild(table.el);

        document.getElementById('qcRefreshBudgets').addEventListener('click', async (e) => {
          e.currentTarget.disabled = true;
          try {
            const res = await API.quota.refreshBudgets();
            toast('success','Budgets re-measured', res && res.message ? res.message : 'Spend re-read and thresholds re-evaluated.');
            table.refresh(); loadKpis();
          } catch (err) { toast('error','Could not re-measure', err.message); }
          finally { e.currentTarget.disabled = false; }
        });

        document.getElementById('qcNewBudget').addEventListener('click', () => {
          openModal({
            title:'New Budget', icon:'creditCard',
            body:`<label class="auth-field"><span>Name</span><input type="text" id="nbName" placeholder="Production monthly"></label>
              <div class="grid g2" style="margin-top:10px">
                <label class="auth-field"><span>Amount (USD)</span><input type="number" id="nbAmount" value="5000" min="1"></label>
                <label class="auth-field"><span>Period</span><select class="filter-select" id="nbPeriod" style="height:34px">
                  ${['Daily','Weekly','Monthly'].map(o=>`<option ${o==='Monthly'?'selected':''}>${o}</option>`).join('')}</select></label>
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
                  await API.quota.budgets.create(payload);
                  toast('success','Budget created', `${payload.name} is now tracked.`);
                  table.refresh(); loadKpis();
                } catch (err) { toast('error','Could not create budget', err.message); }
              }},
              {label:'Cancel'},
            ],
          });
        });
      }

      function editThresholds(r){
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
          const points = f.points || [];
          const chart = points.length ? lineChart({
            series:[
              { name:'Actual', color:'purple', points: points.map(p=>p.actual_usd == null ? null : p.actual_usd), area:true },
              { name:'Forecast', color:'purple', dashed:true, points: points.map(p=>p.forecast_usd == null ? null : p.forecast_usd) },
            ],
            xLabels: points.map(p=>p.label), h:230, zeroBase:true, yFmt:v=>'$'+fmtNum(v),
          }) : `<div class="empty-state">${ICONS.chart}<div class="es-title">Not enough history to forecast yet</div></div>`;

          return card('Spend Forecast', `${chart}
            <div class="legend inline" style="margin-top:6px">
              <span class="legend-item"><span class="sw" style="background:#6D4AEF"></span><span class="lg-label">Actual</span></span>
              <span class="legend-item"><span class="sw" style="background:#6D4AEF;opacity:.5"></span><span class="lg-label">Forecast</span></span>
            </div>
            <div class="grid g3" style="margin-top:14px">
              ${kv([['Method', esc(f.method)],['Days elapsed', String(f.days_elapsed)],['Days remaining', String(f.days_remaining)]])}
              ${kv([['Observed spend', money(f.observed_spend_usd)],['Projected spend', money(f.projected_spend_usd)],['Budget', money(f.budget_usd)]])}
              ${kv([['Projected utilisation', pct(f.projected_utilization_percent)],
                    ['Confidence', `±${money(f.confidence_interval_usd)} at ${pct(f.confidence_level*100,0)}`],
                    ['Daily growth', f.daily_growth_usd==null?dash:money(f.daily_growth_usd,2)]])}
            </div>
            ${f.highest_growth_driver ? `<div class="scan-note" style="margin-top:12px">${ICONS.trendUp} Fastest-growing driver: <b>${esc(f.highest_growth_driver)}</b>, up ${money(f.highest_growth_delta_usd,2)} over the period.</div>` : ''}
            ${f.anomalies_detected ? `<div class="scan-note" style="margin-top:8px">${ICONS.alert} ${f.anomalies_detected} spend anomal${f.anomalies_detected===1?'y':'ies'} detected${(f.anomaly_days||[]).length?` on ${f.anomaly_days.map(esc).join(', ')}`:''}.</div>` : ''}`,
            `<span class="badge bg-purple">Projected ${esc(money(f.projected_spend_usd))}</span>`);
        }, 'the forecast');
      }
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
        try { await API.memory.export(table ? table.params() : {}); toast('success','Export complete','Memory stores exported to CSV.'); }
        catch (err) { toast('error','Export failed', err.message); }
      });
      document.getElementById('msCreate').addEventListener('click', createStore);

      function renderTab(i){
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
            { key:'record_count', label:'Records', align:'right', cls:'num', render:r=>fmtNum(r.record_count) },
            { key:'usage_percent', label:'Usage', render:r=>barPct(r.usage_percent, r.usage_percent>=85?'red':r.usage_percent>=70?'amber':'green') },
            { key:'active_session_count', label:'Sessions', align:'right', cls:'num', render:r=>fmtFull(r.active_session_count) },
            { key:'avg_retrieval_latency_ms', label:'Latency', align:'right', cls:'num', render:r=>r.avg_retrieval_latency_ms==null?dash:Math.round(r.avg_retrieval_latency_ms)+'ms' },
            { key:'retention_policy', label:'Retention', render:r=>r.retention_policy?esc(r.retention_policy):dash },
            { key:'status', label:'Status', render:r=>statusText(r.status) },
          ],
          rowId:'id', itemName:'memory stores', pageSize:10, emptyText:'No memory stores yet',
          filters:[
            {key:'store_type', label:'Type', param:'store_type', options:['Conversation','Vector','Key-Value','Session','Long-term'], allLabel:'All Types'},
            {key:'environment', label:'Environment', param:'environment', options:['Production','Staging','Development'], allLabel:'All Environments'},
            {key:'status', label:'Status', param:'status', options:['Active','Paused','Degraded'], allLabel:'All'},
          ],
          source: (p) => API.memory.list(p),
          exportSource: (p) => API.memory.export(p),
          autoSelectFirst: true,
          onSelect: showStore,
          rowActions: r => [
            {label:'View Records', icon:'database', onClick:()=>viewRecords(r)},
            {label:'Update Retention Policy', icon:'settings', onClick:()=>updateRetention(r)},
            {label:'Create Backup', icon:'save', onClick:()=>createBackup(r)},
            {label:'Restore from Backup', icon:'refresh', onClick:()=>restoreBackup(r)},
            {sep:true},
            {label:'Purge Data', icon:'trash', danger:true, onClick:()=>purgeStore(r)},
          ],
        });
        const wrap = document.getElementById('msTableWrap');
        wrap.appendChild(table.filterEl);
        wrap.appendChild(table.el);
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
            ['Records', fmtNum(r.record_count)],
            ['Usage', barPct(r.usage_percent, r.usage_percent>=85?'red':'green')],
            ['Thread-backed', r.thread_backed?'<span class="st-green">Yes</span>':'No'],
          ]))}
          ${inspSection('Activity','activity', kv([
            ['Active sessions', fmtFull(r.active_session_count)],
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
              { key:'session_id', label:'Session', render:r=>`<span class="mono">${esc(String(r.session_id).slice(0,18))}…</span>` },
              { key:'agent_name', label:'Agent', render:r=>r.agent_id?`<span class="link" data-nav="agent/${esc(r.agent_id)}">${esc(r.agent_name)}</span>`:esc(r.agent_name||'—') },
              { key:'user', label:'User', render:r=>r.user?esc(r.user):dash },
              { key:'turns', label:'Turns', align:'right', cls:'num', render:r=>fmtFull(r.turns) },
              { key:'duration_ms', label:'Duration', align:'right', cls:'num', render:r=>r.duration_ms==null?dash:(r.duration_ms/1000).toFixed(1)+'s' },
              { key:'started_at', label:'Started', render:r=>`<span class="dim nowrap">${when(r.started_at)}</span>` },
              { key:'last_activity_at', label:'Last Activity', render:r=>`<span class="dim nowrap">${when(r.last_activity_at)}</span>` },
              { key:'state', label:'State', render:r=>statusText(r.state) },
            ],
            rowId:'session_id',
          },
          'agent-state': {
            fetch: (p) => API.memory.agentState(p), name:'agent state records', empty:'No agent state recorded',
            columns:[
              { key:'agent_name', label:'Agent', render:r=>r.agent_id?`<span class="link" data-nav="agent/${esc(r.agent_id)}">${esc(r.agent_name||r.agent_id)}</span>`:esc(r.agent_name||'—') },
              { key:'store_name', label:'Store', render:r=>esc(r.store_name||'—') },
              { key:'record_count', label:'Records', align:'right', cls:'num', render:r=>fmtNum(r.record_count) },
              { key:'last_updated_at', label:'Updated', render:r=>`<span class="dim nowrap">${when(r.last_updated_at)}</span>` },
            ],
            rowId:'agent_id',
          },
          'conversations': {
            fetch: (p) => API.memory.conversations(p), name:'conversations', empty:'No conversation state recorded',
            columns:[
              { key:'thread_id', label:'Thread', render:r=>`<span class="mono">${esc(String(r.thread_id||r.id||'').slice(0,18))}…</span>` },
              { key:'agent_name', label:'Agent', render:r=>esc(r.agent_name||'—') },
              { key:'turns', label:'Turns', align:'right', cls:'num', render:r=>fmtFull(r.turns) },
              { key:'last_activity_at', label:'Last Activity', render:r=>`<span class="dim nowrap">${when(r.last_activity_at||r.updated_at)}</span>` },
              { key:'status', label:'Status', render:r=>r.status?statusText(r.status):dash },
            ],
            rowId:'thread_id',
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
            { key:'created_at', label:'Taken', render:r=>`<span class="dim nowrap">${when(r.created_at)}</span>` },
            { key:'store_name', label:'Store', render:r=>esc(r.store_name||'—') },
            { key:'kind', label:'Kind', render:r=>badge(r.kind) },
            { key:'record_count', label:'Records', align:'right', cls:'num', render:r=>fmtNum(r.record_count) },
            { key:'payload_bytes', label:'Size', align:'right', cls:'num', render:r=>r.payload_bytes==null?dash:U.fmtBytes(r.payload_bytes) },
            { key:'created_by', label:'By', render:r=>r.created_by?esc(r.created_by):dash },
            { key:'status', label:'Status', render:r=>statusText(r.status) },
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
        openModal({
          title:'Update Retention Policy — ' + r.name, icon:'settings',
          body:`<label class="auth-field"><span>Policy name</span><input type="text" id="urPolicy" value="${esc(r.retention_policy||'')}" placeholder="e.g. 90-day rolling"></label>
            <label class="auth-field" style="margin-top:10px"><span>Retention (days)</span>
              <input type="number" id="urDays" value="${r.retention_days == null ? '' : r.retention_days}" min="1" placeholder="Leave blank to keep indefinitely"></label>
            <div class="small muted" style="margin-top:8px">Records older than the retention window become eligible for purge. Changing this does not delete anything on its own.</div>`,
          footer:[
            {label:'Save Policy', cls:'primary', onClick: async (close, modal) => {
              const days = modal.querySelector('#urDays').value;
              const payload = { policy: modal.querySelector('#urPolicy').value.trim() || null,
                                retention_days: days === '' ? null : Number(days) };
              close();
              try {
                await API.memory.retention(r.id, payload);
                toast('success','Retention updated', r.name + ' saved.');
                if(table) table.refresh();
                loadKpis();
              } catch (err) { toast('error','Could not update retention', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      function purgeStore(r){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Purging a store requires the operator role.');
          return;
        }
        confirmModal({
          title:'Purge Data', confirmLabel:'Purge', danger:true,
          msg:`Purge expired records from “${r.name}”? This deletes data permanently and is written to the audit trail.`,
          onConfirm: async () => {
            try {
              const res = await API.memory.purge(r.id, {});
              toast('success','Purge complete', res && res.message ? res.message : 'Expired records removed.');
              if(table) table.refresh();
              loadKpis();
            } catch (err) { toast('error','Could not purge', err.message); }
          },
        });
      }

      function createBackup(r){
        openModal({
          title:'Create Backup — ' + r.name, icon:'save',
          body:`<div class="small muted">A backup captures the store's current records and metadata. It can be restored from the Backups tab.</div>`,
          footer:[
            {label:'Create Backup', cls:'primary', onClick: async (close) => {
              close();
              try {
                const res = await API.memory.backup(r.id);
                toast('success','Backup created', res && res.record_count != null
                  ? `${fmtNum(res.record_count)} record(s) captured.` : 'Backup captured.');
                if(table) table.refresh();
                loadKpis();
              } catch (err) { toast('error','Could not create backup', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      function restoreBackup(r){
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
                  host.innerHTML = `<div class="empty-state">${ICONS.save}<div class="es-title">No backups for this store</div><div>Create one first.</div></div>`;
                  return;
                }
                host.innerHTML = `<label class="auth-field"><span>Backup to restore</span>
                  <select class="filter-select" id="rbPick" style="height:34px">${rows.map(b=>
                    `<option value="${esc(b.id)}">${esc(fmtDateTime(ts(b.created_at)))} — ${fmtNum(b.record_count)} records (${esc(b.kind)})</option>`).join('')}</select></label>
                  <div class="small muted" style="margin-top:10px">Restoring replaces the store's current contents with the backup's.</div>`;
                const foot = modal.querySelector('.modal-foot');
                const btn = document.createElement('button');
                btn.className = 'btn primary';
                btn.textContent = 'Restore';
                btn.addEventListener('click', async () => {
                  const backupId = modal.querySelector('#rbPick').value;
                  modal.querySelector('[data-mclose]').click();
                  try {
                    await API.memory.restore(r.id, { backup_id: backupId });
                    toast('success','Restore complete', r.name + ' restored from backup.');
                    if(table) table.refresh();
                    loadKpis();
                  } catch (err) { toast('error','Could not restore', err.message); }
                });
                foot.insertBefore(btn, foot.firstChild);
              })
              .catch(err => { host.innerHTML=''; host.appendChild(screenError(err, null, 'the backup list')); });
          },
        });
      }

      function createStore(){
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
              <input type="number" id="csDays" placeholder="Leave blank to keep indefinitely" min="1"></label>`,
          footer:[
            {label:'Create Store', cls:'primary', onClick: async (close, modal) => {
              const days = modal.querySelector('#csDays').value;
              const payload = {
                name: modal.querySelector('#csName').value.trim(),
                store_type: modal.querySelector('#csType').value,
                environment: modal.querySelector('#csEnv').value,
                retention_days: days === '' ? null : Number(days),
              };
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
        try { await API.deployments.export(table ? table.params() : {}); toast('success','Export complete','Deployments exported to CSV.'); }
        catch (err) { toast('error','Export failed', err.message); }
      });
      document.getElementById('dpCreate').addEventListener('click', createDeployment);

      function renderTab(i){
        body.innerHTML = '';
        table = null;
        if(i === 0) return environmentsTab();
        if(i === 1) return deploymentsTab({});
        if(i === 2) return approvalsTab();
        return deploymentsTab({ terminal: true });
      }

      function environmentsTab(){
        table = dataTable({
          columns:[
            { key:'name', label:'Environment', render:r=>entityCell(r.name, r.description || r.env_type, 'layers',
                r.env_type==='Production'?'green':r.env_type==='Staging'?'cyan':'purple') },
            { key:'env_type', label:'Type', render:r=>badge(r.env_type) },
            { key:'region', label:'Region', render:r=>r.region?esc(r.region):dash },
            { key:'status', label:'Status', render:r=>statusText(r.status) },
            { key:'active_deployment_count', label:'Active Deployments', align:'right', cls:'num', render:r=>fmtFull(r.active_deployment_count) },
            { key:'health', label:'Health', render:r=>r.health==null?dash:barPct(r.health, r.health>=90?'green':r.health>=70?'amber':'red') },
            { key:'last_deployment_at', label:'Last Deployment', render:r=>`<span class="dim nowrap">${when(r.last_deployment_at)}</span>` },
          ],
          rowId:'id', itemName:'environments', pageSize:10, emptyText:'No environments registered yet',
          source: (p) => API.deployments.environments(p),
          rowActions: r => [
            {label:'Restart Services', icon:'refresh', onClick:()=>restartEnvironment(r)},
            {label:'Environment Settings', icon:'settings', onClick:()=>environmentSettings(r)},
          ],
        });
        body.appendChild(table.el);
      }

      function restartEnvironment(r){
        confirmModal({
          title:'Restart Services', confirmLabel:'Restart', danger:true,
          msg:`Restart every service in “${r.name}”? In-flight requests to agents in this environment will fail.`,
          onConfirm: async () => {
            try {
              const res = await API.post(`/environments/${encodeURIComponent(r.id)}/restart`, {});
              toast('success','Restart requested', res && res.message ? res.message : `${r.name} is restarting.`);
              if(table) table.refresh();
            } catch (err) { toast('error','Could not restart', err.message); }
          },
        });
      }

      function environmentSettings(r){
        openModal({
          title:'Environment Settings — ' + r.name, icon:'settings',
          body:`<div class="grid g2">
              <label class="auth-field"><span>Region</span><input type="text" id="esRegion" value="${esc(r.region||'')}"></label>
              <label class="auth-field"><span>Status</span><select class="filter-select" id="esStatus" style="height:34px">
                ${['Healthy','Degraded','Offline'].map(o=>`<option ${o===r.status?'selected':''}>${o}</option>`).join('')}</select></label>
            </div>
            <label class="auth-field" style="margin-top:10px"><span>Description</span><input type="text" id="esDesc" value="${esc(r.description||'')}"></label>`,
          footer:[
            {label:'Save', cls:'primary', onClick: async (close, modal) => {
              const payload = { region: modal.querySelector('#esRegion').value.trim() || null,
                                status: modal.querySelector('#esStatus').value,
                                description: modal.querySelector('#esDesc').value.trim() || null };
              close();
              try {
                await API.patch(`/environments/${encodeURIComponent(r.id)}`, payload);
                toast('success','Environment updated', r.name + ' saved.');
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
            { key:'environment_name', label:'Environment', render:r=>r.environment_name?badge(r.environment_name):dash },
            { key:'strategy', label:'Strategy', render:r=>badge(r.strategy) },
            { key:'status', label:'Status', render:r=>statusText(r.status) },
            { key:'health', label:'Health', render:r=>r.health==null?dash:barPct(r.health, r.health>=90?'green':'amber') },
            { key:'duration_label', label:'Duration', align:'right', cls:'num', render:r=>r.duration_label?esc(r.duration_label):dash },
            { key:'started_at', label:'Started', render:r=>`<span class="dim nowrap">${when(r.started_at)}</span>` },
          ],
          rowId:'id', itemName:'deployments', pageSize:12,
          emptyText: extra.terminal ? 'No completed deployments yet' : 'No deployments yet',
          extraParams: extra.terminal ? { terminal: true } : null,
          filters:[
            {key:'status', label:'Status', param:'status', options:['Queued','Running','Succeeded','Failed','Halted','Rolled Back'], allLabel:'All Statuses'},
            {key:'strategy', label:'Strategy', param:'strategy', options:['Rolling','Blue-Green','Canary','Recreate'], allLabel:'All Strategies'},
          ],
          source: (p) => API.deployments.list(p),
          exportSource: (p) => API.deployments.export(p),
          autoSelectFirst: true,
          onSelect: showDeployment,
          rowActions: r => {
            const actions = [];
            if(!r.is_terminal){
              actions.push({label:'Halt Deployment', icon:'pause', danger:true, onClick:()=>act(r,'halt','Halted')});
              actions.push({label:'Approve', icon:'stamp', onClick:()=>act(r,'approve','Approved')});
            }
            actions.push({label:'Promote', icon:'trendUp', onClick:()=>promote(r)});
            if(r.is_terminal) actions.push({label:'Roll Back', icon:'refresh', danger:true, onClick:()=>act(r,'rollback','Rolled back')});
            return actions;
          },
        });
        const wrap = document.getElementById('dpTableWrap');
        wrap.appendChild(table.filterEl);
        wrap.appendChild(table.el);
      }

      async function act(r, verb, past){
        try {
          await API.deployments[verb](r.id, {});
          toast('success', past, `${r.deployment_ref} ${past.toLowerCase()}.`);
          if(table) table.refresh();
          loadKpis();
        } catch (err) { toast('error', `Could not ${verb}`, err.message); }
      }

      async function promote(r){
        let envs;
        try { envs = await API.deployments.environments({ page_size: 50 }); }
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
                await API.deployments.promote(r.id, { environment_id: envId });
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
        const state = s.status === 'Failed' ? 'fail' : s.status === 'Succeeded' ? 'done' : 'run';
        const dur = s.started_at && s.finished_at
          ? Math.round((new Date(s.finished_at) - new Date(s.started_at)) / 1000) + 's' : '';
        return `<div class="pipe-step">
          <div class="pipe-dot ${state}">${state==='fail'?ICONS.x:state==='done'?ICONS.check:ICONS.clock}</div>
          <div class="pipe-body"><div class="pipe-title"><span>${esc(s.name)}</span><span class="faint num">${dur}</span></div>
          <div class="pipe-sub">${esc(s.log || s.status)}</div></div></div>`;
      }

      function approvalsTab(){
        table = dataTable({
          columns:[
            { key:'id', label:'Request', render:r=>`<span class="mono">${esc(String(r.id).slice(0,10))}…</span>` },
            { key:'resource', label:'Resource', render:r=>esc(r.resource||'—') },
            { key:'requested_by', label:'Requested By', render:r=>esc(r.requested_by||'—') },
            { key:'risk', label:'Risk', render:r=>r.risk?C.riskBadge(r.risk):dash },
            { key:'status', label:'Status', render:r=>statusText(r.status) },
            { key:'created_at', label:'Raised', render:r=>`<span class="dim nowrap">${when(r.created_at)}</span>` },
          ],
          rowId:'id', itemName:'approval requests', pageSize:10,
          emptyText:'No deployment approvals raised',
          source: (p) => API.deployments.approvals(p),
          rowActions: r => [{label:'Open in Approvals & Audit', icon:'stamp', onClick:()=>APP.go('approvals')}],
        });
        body.appendChild(table.el);
      }

      async function createDeployment(){
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Creating a deployment requires the operator role.');
          return;
        }
        let envs, agents;
        try {
          [envs, agents] = await Promise.all([
            API.deployments.environments({ page_size: 50 }),
            API.deployments.agents({ page_size: 50 }),
          ]);
        } catch (err) { toast('error','Could not load targets', err.message); return; }

        if(!envs.items.length){ toast('warn','No environments','Register an environment before deploying.'); return; }
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
              const payload = {
                agent_id: modal.querySelector('#cdAgent').value,
                environment_id: modal.querySelector('#cdEnv').value,
                version: modal.querySelector('#cdVersion').value.trim(),
                strategy: modal.querySelector('#cdStrategy').value,
                notes: modal.querySelector('#cdNotes').value.trim() || null,
              };
              close();
              try {
                const created = await API.deployments.create(payload);
                toast('success','Deployment started', `${created.deployment_ref} is running.`);
                watchDeployment(created);
              } catch (err) { toast('error','Could not start deployment', err.message); }
            }},
            {label:'Cancel'},
          ],
        });
      }

      /** Follow the server's pipeline. The stages are its, not ours. */
      function watchDeployment(deployment){
        openModal({
          title:`Deploying ${deployment.version} → ${deployment.environment_name || 'environment'}`, icon:'rocket',
          body:`<div id="dpLive"><div class="card-loading" style="height:160px"></div></div>`,
          footer:[{label:'Close'}],
        });
        const host = document.getElementById('dpLive');

        function paint(stages){
          if(!host || !host.isConnected) return;
          host.innerHTML = stages.length
            ? `<div class="pipe">${stages.map(stageHtml).join('')}</div>`
            : `<div class="empty-state">${ICONS.rocket}<div class="es-title">Waiting for the first stage…</div></div>`;
        }

        if(liveStream) liveStream.close();
        liveStream = API.deployments.stream(deployment.id, {
          events: {
            stage: (frame) => { if(frame && frame.stages) paint(frame.stages); else refreshStages(); },
            done: () => {
              refreshStages();
              if(table) table.refresh();
              loadKpis();
              toast('success','Deployment finished', `${deployment.deployment_ref} completed.`);
              if(liveStream){ liveStream.close(); liveStream = null; }
            },
          },
          onMessage: () => refreshStages(),
          onError: () => refreshStages(),
        });

        function refreshStages(){
          API.deployments.stages(deployment.id)
            .then(s => paint(s.items || s))
            .catch(() => {});
        }
        refreshStages();
      }

      this.cleanup = () => { if(liveStream){ liveStream.close(); liveStream = null; } };
    },
  };
})();
