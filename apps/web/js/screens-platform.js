/* Fulcrum Ops — PLATFORM screens: Live Runs, Replay Studio, Metrics
 *
 * Everything here is telemetry: it comes from the control plane's run and
 * metric endpoints, which read the engine behind them. Nothing on these screens
 * is generated in the browser. When the telemetry backend cannot answer, the
 * screen says so rather than showing a zero.
 */
(function(){
  'use strict';
  window.SCREENS = window.SCREENS || {};
  const { esc, relTime, fmtTime, fmtNum, fmtFull, fmtMoney, sparkline, lineChart, donut } = U;
  const { badge, statusText, riskBadge, platformCell, kpiRow, kpiCard, kpiSkeleton, miniKpi, dataTable,
          pageHead, searchBox, inspSection, kv, toast, openModal, screenError } = C;

  const TOOL_LOGO = { 'sharepoint':'sharepoint','outlook':'outlook','graph':'m365','sql':'sql','mcp':'mcp',
    'vector':'vector','search':'vector','insights':'appinsights','blob':'azureblob','keyvault':'keyvault' };

  /** Pick a logo for a tool by what its name contains; unknown tools get the generic mark. */
  function toolLogo(name){
    const n = String(name || '').toLowerCase();
    const hit = Object.keys(TOOL_LOGO).find(k => n.includes(k));
    return LOGOS[hit ? TOOL_LOGO[hit] : 'custom'];
  }

  function toolChips(tools, max){
    const list = tools || [];
    if(!list.length) return '<span class="faint">—</span>';
    max = max || 2;
    const shown = list.slice(0, max);
    const extra = list.length - shown.length;
    return `<span class="flex" style="gap:4px">${shown.map(t=>`<span title="${esc(t)}" style="width:20px;height:20px;border-radius:5px;background:var(--panel-3);display:inline-flex;align-items:center;justify-content:center;padding:2.5px">${toolLogo(t)}</span>`).join('')}${extra>0?`<span class="badge bg-gray">+${extra}</span>`:''}</span>`;
  }

  const dash = '<span class="faint">—</span>';
  const num = (v, digits) => v == null ? dash : (digits != null ? Number(v).toFixed(digits) : fmtFull(v));
  const secs = (v) => v == null ? dash : Number(v).toFixed(2) + 's';
  const ms = (v) => v == null ? dash : Math.round(v) + ' ms';
  const pct = (v, digits) => v == null ? dash : Number(v).toFixed(digits == null ? 1 : digits) + '%';
  const ts = (v) => v ? new Date(v).getTime() : null;

  /** A KPI card built from the server's MetricKpi, which carries its own display strings. */
  function serverKpi(k, icon, color){
    return {
      label: k.label, value: k.display, icon: icon || k.icon, color: color || k.color,
      delta: k.delta_display || null,
      dir: k.direction === 'flat' ? null : k.direction,
      good: k.good,
      vs: k.comparison,
      sub: k.delta_display ? null : k.sub,
    };
  }

  /* ================= LIVE RUNS ================= */
  SCREENS['live-runs'] = {
    title:'Live Runs',
    render(main){
      let liveOn = true, runStream = null, currentRunId = null;

      main.innerHTML = `
        ${pageHead({ title:'Live Runs', sub:'Real-time observability of agent executions across every connected platform.',
          actions:`${searchBox('lrSearch','Search runs…')}
            <span class="live-pill" id="livePill"><span class="dot pulse" style="background:currentColor"></span>LIVE</span>
            <button class="btn orange" id="btnRun">${ICONS.play}Run</button>` })}
        <div class="chip-row" id="lrChips"></div>
        <div id="lrKpis">${kpiSkeleton(['Total Runs','Success Rate','Avg Latency','Policy Violations'])}</div>
        <div class="mini-kpi-row" id="lrMini"></div>
        <div id="lrScanNote"></div>
        <div class="with-inspector" id="lrLayout">
          <div id="lrTableWrap"></div>
          <div class="inspector" id="lrInspector"></div>
        </div>`;

      // ---- connection chips: which platforms are actually reporting ----
      API.connections.list({ page_size: 8, sort: '-updated_at' })
        .then(page => {
          const chips = document.getElementById('lrChips');
          if(!chips) return;
          if(!page.items.length){
            chips.innerHTML = `<span class="faint small">No platform connections yet — <span class="link" data-nav="connections">add one</span> so agents can report in.</span>`;
            return;
          }
          chips.innerHTML = page.items.map(c=>
            `<span class="conn-chip" data-nav="connections"><span class="logo">${platformLogo(c.platform)}</span>${esc(c.name)}<span class="stat"><span class="dot"></span>${esc(c.status)}</span></span>`
          ).join('');
        })
        .catch(()=>{ const el = document.getElementById('lrChips'); if(el) el.innerHTML = ''; });

      function platformLogo(platform){
        const map = { 'Azure AI Foundry':'foundry', 'Copilot Studio':'copilot', 'M365 Copilot':'m365',
                      'Power Platform':'power', 'Custom Agent':'custom' };
        return LOGOS[map[platform] || 'custom'];
      }

      // ---- KPI cards + the four sparkline KPIs -------------------------
      function loadSummary(){
        const host = document.getElementById('lrKpis');
        const mini = document.getElementById('lrMini');
        const note = document.getElementById('lrScanNote');
        if(!host) return;
        const params = table ? table.params() : {};
        API.runs.summary({ time_range: params.time_range, tenant: params.tenant, source: params.source })
          .then(s => {
            if(!document.getElementById('lrKpis')) return;
            host.innerHTML = kpiRow([
              { label:'Total Runs', value:fmtFull(s.total_runs), icon:'activity', color:'purple',
                delta: s.total_runs_delta_percent == null ? null : Math.abs(s.total_runs_delta_percent).toFixed(1)+'%',
                dir: deltaDir(s.total_runs_delta_percent), good: s.total_runs_delta_percent >= 0, vs:'vs previous period' },
              { label:'Success Rate', value: s.success_rate == null ? '—' : pct(s.success_rate), icon:'target', color:'green',
                delta: s.success_rate_delta_points == null ? null : Math.abs(s.success_rate_delta_points).toFixed(1)+' pp',
                dir: deltaDir(s.success_rate_delta_points), good: s.success_rate_delta_points >= 0, vs:'vs previous period' },
              { label:'Avg Latency', value: secs(s.avg_latency_seconds), icon:'clock', color:'amber',
                delta: s.avg_latency_delta_seconds == null ? null : Math.abs(s.avg_latency_delta_seconds).toFixed(2)+'s',
                dir: deltaDir(s.avg_latency_delta_seconds), good: s.avg_latency_delta_seconds <= 0, vs:'vs previous period' },
              { label:'Policy Violations', value:fmtFull(s.policy_violations), icon:'shield', color:'red',
                delta: s.policy_violations_delta_percent == null ? null : Math.abs(s.policy_violations_delta_percent).toFixed(1)+'%',
                dir: deltaDir(s.policy_violations_delta_percent), good: s.policy_violations_delta_percent <= 0, vs:'vs previous period' },
            ]);
            if(mini) mini.innerHTML = [s.tokens_used, s.estimated_cost, s.fallback_rate, s.human_escalations]
              .filter(Boolean)
              .map((k, i) => miniKpi({
                label: k.label,
                value: formatSpark(k),
                spark: (k.series || []).map(p => p.value),
                color: ['green','orange','orange','purple'][i],
              })).join('');
            if(note) note.innerHTML = s.scan && s.scan.truncated
              ? `<div class="scan-note">${ICONS.info} Showing the most recent ${fmtFull(s.scan.runs_scanned)} runs across ${s.scan.agents_scanned} of ${s.scan.agents_total} agents — the window was capped, so totals below are a floor, not a complete count.</div>`
              : '';
          })
          .catch(err => {
            host.innerHTML = '';
            host.appendChild(screenError(err, loadSummary, 'the run summary'));
            if(mini) mini.innerHTML = '';
          });
      }

      function deltaDir(v){ return v == null || v === 0 ? null : (v > 0 ? 'up' : 'down'); }

      function formatSpark(k){
        if(k.unit === 'usd') return fmtMoney(k.value, 0);
        if(k.unit === 'percent') return pct(k.value);
        if(k.unit === 'tokens') return fmtNum(k.value);
        return fmtFull(k.value);
      }

      // ---- the run table ----------------------------------------------
      const cols = [
        { key:'id', label:'Run ID', render:r=>`<span class="mono">${esc(String(r.id).slice(0,9))}…</span>` },
        { key:'source', label:'Source', render:r=>r.source?platformCell(r.source):dash },
        { key:'agent', label:'Agent', render:r=>r.agent_id
            ? `<span class="link" data-nav="agent/${esc(r.agent_id)}" onclick="event.stopPropagation()">${esc(r.agent)}</span>`
            : esc(r.agent || '—') },
        { key:'status', label:'Status', render:r=>statusText(r.status) },
        { key:'model', label:'Model', render:r=>r.model?`<span class="dim">${esc(r.model)}</span>`:dash },
        { key:'input_preview', label:'Input Preview', sortable:false, render:r=>`<span class="dim" style="max-width:230px;display:inline-block;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom">${esc(r.input_preview||'')}</span>` },
        { key:'tools', label:'Tools', sortable:false, render:r=>toolChips(r.tools) },
        { key:'tokens', label:'Tokens', align:'right', cls:'num', render:r=>fmtFull(r.tokens) },
        { key:'cost', label:'Cost', align:'right', cls:'num', render:r=>fmtMoney(r.cost,3) },
        { key:'duration_seconds', label:'Duration', align:'right', cls:'num', render:r=>secs(r.duration_seconds) },
        { key:'confidence', label:'Confidence', align:'right', cls:'num', render:r=>num(r.confidence,2) },
        { key:'risk', label:'Risk', render:r=>r.risk?riskBadge(r.risk):dash },
        { key:'policy', label:'Policy', render:r=>badge(r.policy) },
        { key:'tenant', label:'Tenant', render:r=>esc(r.tenant||'—') },
        { key:'occurred_at', label:'Time', render:r=>`<span class="dim nowrap">${relTime(ts(r.occurred_at))}</span>` },
      ];

      const table = dataTable({
        columns: cols, rowId:'id', pageSize:25, pageSizes:[10,25,50],
        itemName:'runs', searchPlaceholder:'Search runs…',
        defaultSort:{ key:'occurred_at', dir:-1 },
        emptyText:'No runs in this window',
        filters:[
          {key:'tenant', label:'Tenant', param:'tenant', options:[], allLabel:'All Tenants'},
          {key:'source', label:'Source', param:'source', options:['Azure AI Foundry','Copilot Studio','M365 Copilot','Power Platform','Custom Agent'], allLabel:'All Sources'},
          {key:'status', label:'Status', param:'status', options:['Completed','Warned','Failed','Running'], allLabel:'All'},
          {key:'risk', label:'Risk', param:'risk', options:['Low','Medium','High'], allLabel:'All'},
          {key:'policy', label:'Policy', param:'policy', options:['Allowed','Warned','Blocked'], allLabel:'All'},
          {key:'time_range', label:'Time Range', param:'time_range', options:['Last hour','Last 6 hours','Last 24 hours'], allLabel:'Last 24 hours'},
        ],
        source: (params) => API.runs.list(params),
        exportSource: (params) => API.runs.export(params),
        onLoad: () => loadSummary(),
        autoSelectFirst: true,
        onSelect: showRun,
        rowActions: r=>[
          {label:'View Full Trace', icon:'activity', onClick:()=>openTrace(r.id)},
          {label:'Open in Replay Studio', icon:'replay', onClick:()=>{ APP.replayRun = r.id; APP.go('replay'); }},
          ...(r.agent_id ? [{label:'View Agent', icon:'bot', onClick:()=>APP.go('agent/'+r.agent_id)}] : []),
          {sep:true},
          {label:'Flag for Review', icon:'flag', onClick:()=>flagRun(r)},
        ],
      });

      const wrap = document.getElementById('lrTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);
      document.getElementById('lrSearch').addEventListener('input', e=>table.search(e.target.value));

      // The tenant list is workspace-specific, so it comes from the summary.
      API.runs.summary({}).then(s => {
        const select = table.filterEl && table.filterEl.querySelector('[data-fi="0"]');
        if(select && (s.tenants||[]).length){
          s.tenants.forEach(t => {
            const opt = document.createElement('option');
            opt.textContent = t; select.appendChild(opt);
          });
        }
      }).catch(()=>{});

      async function flagRun(r){
        try {
          const result = await API.runs.flag(r.id, { reason: 'Flagged from Live Runs' });
          toast('warn','Flagged for review', `${String(result.run_id).slice(0,12)}… routed to the review queue.`);
          table.refresh();
        } catch (err) {
          toast('error','Could not flag run', err.message);
        }
      }

      // ---- inspector ----------------------------------------------------
      function showRun(row){
        const insp = document.getElementById('lrInspector');
        if(!insp || !row) return;
        currentRunId = row.id;
        document.getElementById('lrLayout').classList.remove('collapsed');
        insp.innerHTML = `<div class="insp-head"><div>
            <div class="insp-title">Run Details</div>
            <div class="insp-sub mono">${esc(row.id)}</div></div>
            <button class="icon-btn insp-close" id="lrInspClose">${ICONS.x}</button></div>
          <div class="card-loading" style="height:220px;margin:12px"></div>`;
        insp.querySelector('#lrInspClose').addEventListener('click', ()=>document.getElementById('lrLayout').classList.add('collapsed'));

        API.runs.get(row.id)
          .then(r => { if(currentRunId === row.id) paintRun(insp, r); })
          .catch(err => {
            if(currentRunId !== row.id) return;
            const holder = insp.querySelector('.card-loading');
            if(holder) holder.replaceWith(screenError(err, ()=>showRun(row), 'this run'));
          });
      }

      function paintRun(insp, r){
        const g = r.guardrails || {}, ret = r.retrieval || {}, err = r.errors || {};
        insp.innerHTML = `
          <div class="insp-head"><div>
            <div class="insp-title">Run Details</div>
            <div class="insp-sub mono">${esc(r.id)}</div></div>
            <button class="icon-btn insp-close" id="lrInspClose">${ICONS.x}</button></div>
          ${inspSection('Execution Summary','activity', kv([
            ['Run ID', `<span class="mono">${esc(String(r.id).slice(0,18))}…</span>`],
            ['Session ID', r.session_id?`<span class="mono">${esc(String(r.session_id).slice(0,18))}</span>`:dash],
            ['Tenant', esc(r.tenant||'—')], ['User', esc(r.user||'—')],
            ['Source', r.source?esc(r.source):dash],
            ['Agent', r.agent_id?`<span class="link" data-nav="agent/${esc(r.agent_id)}">${esc(r.agent)}</span>`:esc(r.agent||'—')],
            ['Environment', r.environment?badge(r.environment):dash],
            ['Status', statusText(r.status)],
            ['Time', `${fmtTime(ts(r.occurred_at))} (${relTime(ts(r.occurred_at))})`],
          ]))}
          ${inspSection('Prompt & Response','chat', `
            <div class="small muted" style="font-weight:700;margin-bottom:3px">USER PROMPT</div>
            <div class="quote">${esc(r.input || '—')}</div>
            <div class="small muted" style="font-weight:700;margin:8px 0 3px">RESPONSE (PREVIEW)</div>
            <div class="quote">${esc(r.response || '—')}</div>
            <button class="link orange" id="lrFullResp">View full response ${ICONS.arrowRight}</button>`)}
          ${inspSection('Model & Cost','cpu', kv([
            ['Model', r.model?esc(r.model):dash],
            ['Input Tokens', fmtFull(r.input_tokens)], ['Output Tokens', fmtFull(r.output_tokens)],
            ['Total Tokens', `<b>${fmtFull(r.tokens)}</b>`], ['Cost', `<b>${fmtMoney(r.cost,3)}</b>`],
            ['Duration', secs(r.duration_seconds)],
            ['Spans', `${fmtFull(r.span_count)} (${fmtFull(r.llm_span_count)} model)`],
          ]))}
          ${inspSection('Tools & Connectors','tool', `<div class="flex flex-wrap" style="gap:6px">${
            (r.tools||[]).map(t=>`<span class="conn-chip" style="padding:5px 10px;font-size:11.5px" data-nav="connectors"><span class="logo" style="width:14px;height:14px">${toolLogo(t)}</span>${esc(t)}</span>`).join('')
            || '<span class="faint">No tools called</span>'}</div>`)}
          ${inspSection('Guardrails & Policy','shieldCheck', kv([
            ['Prompt Injection Check', g.prompt_injection_check?statusText(g.prompt_injection_check):dash],
            ['PII Detection', g.pii_detection?statusText(g.pii_detection):dash],
            ['Final Policy Result', badge(g.final_policy || r.policy)],
          ]) + ((g.verdicts||[]).length ? `<div class="flex flex-wrap" style="gap:5px;margin-top:8px">${
            g.verdicts.map(v=>badge(`${v.name}: ${v.result}`, v.result==='Passed'?'green':'red')).join('')}</div>` : ''))}
          ${inspSection('Retrieval & Citations','book', kv([
            ['Retrieved Documents', ret.documents == null ? dash : String(ret.documents)],
            ['Grounding Score', ret.grounding_score == null ? dash : U.barPct(ret.grounding_score*100,'orange', ret.grounding_score.toFixed(2))],
            ['Citation Accuracy', ret.citation_accuracy == null ? dash : U.barPct(ret.citation_accuracy,'orange', pct(ret.citation_accuracy,0))],
            ['Source Freshness', ret.source_freshness == null ? dash : U.barPct(ret.source_freshness,'orange', pct(ret.source_freshness,0))],
          ]))}
          ${inspSection('Errors & Fallbacks','alert', kv([
            ['Retry Count', String(err.retry_count == null ? 0 : err.retry_count)],
            ['Fallback Used', err.fallback_used ? 'Yes' : 'No'],
            ['Escalated', err.escalated ? '<span class="st-amber">Yes</span>' : 'No'],
            ...(err.message ? [['Message', esc(err.message)]] : []),
          ]))}
          ${(r.feedback_scores||[]).length ? inspSection('Feedback Scores','star',
            kv(r.feedback_scores.map(s=>[esc(s.name), num(s.value,2)]))) : ''}
          ${inspSection('Audit & Replay','history', kv([
            ['Trace Available', r.trace_available?'<span class="st-green">Yes</span>':'<span class="faint">No</span>'],
            ['Replay Supported', r.replay_supported?'<span class="st-green">Yes</span>':'<span class="faint">No</span>'],
            ['Flagged for Review', r.flagged_for_review?'<span class="st-amber">Yes</span>':'No'],
          ]) + `<div class="flex" style="margin-top:9px;gap:8px">
            <button class="btn sm" id="lrTrace" ${r.trace_available?'':'disabled title="This run has no recorded spans"'}>${ICONS.activity}View Full Trace</button>
            <button class="btn sm" id="lrReplay" ${r.replay_supported?'':'disabled title="This run cannot be replayed"'}>${ICONS.replay}Replay</button></div>`)}`;

        insp.querySelector('#lrInspClose').addEventListener('click', ()=>document.getElementById('lrLayout').classList.add('collapsed'));
        insp.querySelector('#lrTrace').addEventListener('click', ()=>openTrace(r.id));
        insp.querySelector('#lrReplay').addEventListener('click', ()=>{ APP.replayRun = r.id; APP.go('replay'); });
        insp.querySelector('#lrFullResp').addEventListener('click', ()=>openFullResponse(r.id));
      }

      function openFullResponse(runId){
        openModal({ title:'Full Response', icon:'chat',
          body:`<div class="card-loading" style="height:160px"></div>`, footer:[{label:'Close'}],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.runs.response(runId)
              .then(r => {
                modal.querySelector('.modal-title').textContent = 'Full Response — ' + String(r.run_id).slice(0,12) + '…';
                body.innerHTML = `<div class="quote" style="font-size:12.5px"><b>${esc(r.agent||'—')}</b> · ${esc(r.model||'—')} · ${fmtTime(ts(r.occurred_at))} · ${fmtFull(r.output_tokens)} tokens · ${fmtFull(r.character_count)} chars</div>
                  <p style="white-space:pre-wrap">${esc(r.response)}</p>`;
              })
              .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, null, 'the response')); });
          },
        });
      }

      function openTrace(runId){
        openModal({ title:'Execution Trace', icon:'activity', wide:true,
          body:`<div class="card-loading" style="height:220px"></div>`,
          footer:[
            {label:'Open in Replay Studio', cls:'primary', onClick:(close)=>{ close(); APP.replayRun = runId; APP.go('replay'); }},
            {label:'Close'},
          ],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.runs.trace(runId)
              .then(t => {
                modal.querySelector('.modal-title').textContent = 'Execution Trace — ' + String(t.run_id).slice(0,14) + '…';
                body.innerHTML = `<div class="flex" style="gap:14px;margin-bottom:14px;flex-wrap:wrap">
                    ${kv([['Agent', esc(t.agent||'—')]])}${kv([['Model', esc(t.model||'—')]])}
                    ${kv([['Duration', secs(t.duration_seconds)]])}${kv([['Status', statusText(t.status)]])}
                    ${kv([['Spans', fmtFull(t.span_count)]])}${kv([['Tokens', fmtFull(t.total_tokens)]])}
                    ${kv([['Cost', fmtMoney(t.total_cost,4)]])}
                  </div>
                  ${t.spans && t.spans.length ? `<div class="pipe">${renderSpans(t.spans, 0)}</div>`
                    : '<div class="empty-state">'+ICONS.search+'<div class="es-title">No spans were recorded for this run</div><div>The agent reported the run but not its internal steps.</div></div>'}`;
              })
              .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, null, 'the execution trace')); });
          },
        });
      }

      function renderSpans(spans, depth){
        return spans.map(s => {
          const state = s.status === 'error' || s.error ? 'fail' : 'done';
          const detail = [s.span_type, s.model, s.tokens ? fmtFull(s.tokens)+' tokens' : null,
                          s.cost ? fmtMoney(s.cost,4) : null, s.error || s.output_preview || s.input_preview]
                         .filter(Boolean).join(' · ');
          return `<div class="pipe-step" style="margin-left:${depth*18}px">
            <div class="pipe-dot ${state}">${state==='fail'?ICONS.x:ICONS.check}</div>
            <div class="pipe-body">
              <div class="pipe-title"><span>${esc(s.name)}</span><span class="faint num">${ms(s.duration_ms)}</span></div>
              <div class="pipe-sub">${esc(detail || '—')}</div>
            </div></div>${s.children && s.children.length ? renderSpans(s.children, depth+1) : ''}`;
        }).join('');
      }

      // ---- the live stream ----------------------------------------------
      const pill = document.getElementById('livePill');

      function setPill(state){
        pill.classList.toggle('paused', state !== 'live');
        pill.innerHTML = state === 'live'
          ? '<span class="dot pulse" style="background:currentColor"></span>LIVE'
          : state === 'reconnecting'
            ? `${ICONS.refresh}<span style="margin-left:2px">RECONNECTING</span>`
            : `${ICONS.pause}<span style="margin-left:2px">PAUSED</span>`;
      }

      function openStream(){
        if(runStream) return;
        const p = table.params();
        runStream = API.runs.stream({
          params: { tenant: p.tenant, source: p.source, status: p.status, risk: p.risk, policy: p.policy },
          events: {
            open: () => setPill('live'),
            run: (frame) => {
              if(!frame || !frame.run || !liveOn) return;
              table.prependRow(frame.run);
              loadSummary();
            },
          },
          onError: () => setPill('reconnecting'),
        });
      }

      function closeStream(){
        if(runStream){ runStream.close(); runStream = null; }
      }

      pill.addEventListener('click', ()=>{
        liveOn = !liveOn;
        if(liveOn){ openStream(); setPill('live'); }
        else { closeStream(); setPill('paused'); }
        toast('info', liveOn?'Live stream resumed':'Live stream paused',
          liveOn?'New runs appear as they are reported.':'The connection is closed until you resume.');
      });
      openStream();
      this.cleanup = closeStream;

      // ---- trigger a run --------------------------------------------------
      document.getElementById('btnRun').addEventListener('click', async () => {
        if(!Store.session.can('operator')){
          toast('error','Not permitted','Starting a run requires the operator role.');
          return;
        }
        let agents;
        try { agents = await API.agents.list({ page_size: 50, status: 'Active' }); }
        catch (err) { toast('error','Could not load agents', err.message); return; }
        if(!agents.items.length){
          toast('warn','No active agents','Register and activate an agent before starting a run.');
          return;
        }
        openModal({
          title:'Start a Run', icon:'play',
          body:`<label class="auth-field"><span>Agent</span>
              <select class="filter-select" id="runAgent" style="height:34px">${
                agents.items.map(a=>`<option value="${esc(a.id)}">${esc(a.name)} — ${esc(a.environment||'')}</option>`).join('')}</select></label>
            <label class="auth-field" style="margin-top:10px"><span>Input</span>
              <input type="text" id="runInput" placeholder="What should the agent be asked?"></label>`,
          footer:[
            {label:'Start Run', cls:'orange', onClick: async (close, modal) => {
              const agentId = modal.querySelector('#runAgent').value;
              const input = modal.querySelector('#runInput').value.trim();
              close();
              try {
                const started = await API.agents.run(agentId, input ? { input } : {});
                toast('success','Run started', `${esc(started.agent || 'Agent')} · ${String(started.run_id||'').slice(0,12)}…`);
                table.refresh();
              } catch (err) {
                toast('error','Could not start the run', err.message);
              }
            }},
            {label:'Cancel'},
          ],
        });
      });
    },
  };

  /* ================= REPLAY STUDIO ================= */
  SCREENS['replay'] = {
    title:'Replay Studio',
    render(main){
      let timer = null;

      main.innerHTML = `
        ${pageHead({title:'Replay Studio', sub:'Step through a recorded execution with its prompt, retrieval, tool and guardrail context.',
          actions:`<select class="filter-select" id="rpPick" style="min-width:290px;height:34px"><option>Loading runs…</option></select>
          <button class="btn primary" id="rpPlay" disabled>${ICONS.play}Play Replay</button>`})}
        <div id="rpBody"><div class="card-loading" style="height:180px"></div></div>`;

      const body = document.getElementById('rpBody');
      const picker = document.getElementById('rpPick');
      const playBtn = document.getElementById('rpPlay');

      /* The run asked for wins, even when it is older than this page.
       *
       * The picker lists recent runs for convenience, but arriving here from
       * Live Runs names one specific run. Falling back to the newest run when
       * the named one is off the page would replay a *different* execution
       * than the one clicked, silently, which is the worst possible failure
       * for a debugging tool. So a named run is fetched on its own and put at
       * the head of the list; only an unnamed arrival defaults to the newest.
       */
      const requested = APP.replayRun;
      Promise.all([
        API.runs.list({ page_size: 30, sort: '-occurred_at' }),
        requested ? API.runs.get(requested).catch(() => null) : Promise.resolve(null),
      ])
        .then(([page, named]) => {
          const items = (page.items || []).slice();
          if(named && !items.some(r => r.id === named.id)) items.unshift(named);

          if(!items.length){
            picker.innerHTML = '<option>No runs recorded yet</option>';
            body.innerHTML = `<div class="empty-state">${ICONS.replay}
              <div class="es-title">Nothing to replay yet</div>
              <div>Once an agent reports a run, it can be replayed step by step here.</div></div>`;
            return;
          }

          // A named run that could not be fetched is said plainly rather than
          // quietly swapped for another one.
          if(requested && !items.some(r => r.id === requested)){
            picker.innerHTML = items.map(o =>
              `<option value="${esc(o.id)}">${esc(o.agent||'Agent')} — ${esc(String(o.id).slice(0,10))}… (${relTime(ts(o.occurred_at))})</option>`
            ).join('');
            picker.addEventListener('change', ()=>load(picker.value));
            body.innerHTML = `<div class="empty-state">${ICONS.alert}
              <div class="es-title">That run could not be loaded</div>
              <div>Run <span class="mono">${esc(String(requested).slice(0,14))}…</span> is not available —
              it may have passed its retention window. Pick another run above.</div></div>`;
            APP.replayRun = null;
            return;
          }

          const wanted = requested || items[0].id;
          picker.innerHTML = items.map(o =>
            `<option value="${esc(o.id)}" ${o.id===wanted?'selected':''}>${esc(o.agent||'Agent')} — ${esc(String(o.id).slice(0,10))}… (${relTime(ts(o.occurred_at))})</option>`
          ).join('');
          picker.addEventListener('change', ()=>load(picker.value));
          load(wanted);
        })
        .catch(err => {
          picker.innerHTML = '<option>Unavailable</option>';
          body.innerHTML = '';
          body.appendChild(screenError(err, ()=>SCREENS['replay'].render(main), 'the run list'));
        });

      function load(runId){
        APP.replayRun = runId;
        playBtn.disabled = true;
        body.innerHTML = '<div class="card-loading" style="height:180px"></div>';
        API.runs.replay(runId)
          .then(session => paint(session))
          .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, ()=>load(runId), 'this replay')); });
      }

      function paint(session){
        const r = session.run || {};
        body.innerHTML = `
          <div class="kpi-row" style="grid-template-columns:repeat(auto-fit,minmax(170px,1fr))">
            ${kpiCard({label:'Run', value:`<span style="font-size:15px" class="mono">${esc(String(r.id||'').slice(0,14))}…</span>`, sub:r.agent||'—', icon:'activity', color:'purple'})}
            ${kpiCard({label:'Status', value:statusText(r.status||'—'), sub:r.model||'—', icon:'target', color:'green'})}
            ${kpiCard({label:'Duration', value:secs(session.total_duration_ms == null ? r.duration_seconds : session.total_duration_ms/1000), sub:`${session.step_count} steps`, icon:'clock', color:'amber'})}
            ${kpiCard({label:'Fidelity', value:session.fidelity == null ? '—' : pct(session.fidelity,0), sub:`${session.captured_steps} of ${session.step_count} steps with full payloads`, icon:'layers', color:'blue'})}
          </div>
          ${session.replayable ? '' : `<div class="scan-note">${ICONS.info} This run was reported without step payloads, so the timeline shows what was recorded and no more.</div>`}
          <div class="card" style="margin-top:14px">
            <div class="card-head"><div class="card-title">Execution Timeline</div>
              <div class="faint small">${session.step_count} steps</div></div>
            <div class="pipe" id="rpSteps">${session.steps.map((s,i)=>stepHtml(s,i)).join('')}</div>
          </div>`;
        playBtn.disabled = !session.steps.length;
        playBtn.onclick = ()=>play(session);
      }

      function stepHtml(s, i){
        const state = s.status === 'error' || s.error ? 'fail' : 'done';
        const facts = [
          s.model, s.tokens ? fmtFull(s.tokens)+' tokens' : null,
          s.cost ? fmtMoney(s.cost,4) : null,
          (s.retrieved_chunks||[]).length ? `${s.retrieved_chunks.length} chunks` : null,
          s.tool_call ? `tool: ${s.tool_call.name}${s.tool_call.ok===false?' (failed)':''}` : null,
          (s.guardrails||[]).length ? `${s.guardrails.length} guardrail checks` : null,
        ].filter(Boolean).join(' · ');
        return `<div class="pipe-step" data-step="${i}">
          <div class="pipe-dot ${state}">${state==='fail'?ICONS.x:ICONS.check}</div>
          <div class="pipe-body">
            <div class="pipe-title"><span>${esc(s.title)}</span><span class="faint num">${s.duration_ms==null?'—':ms(s.duration_ms)}</span></div>
            <div class="pipe-sub">${esc(s.detail || '')}${facts?` <span class="faint">· ${esc(facts)}</span>`:''}</div>
            ${s.prompt?`<div class="quote small" style="margin-top:6px">${esc(s.prompt)}</div>`:''}
            ${s.response?`<div class="quote small" style="margin-top:6px">${esc(s.response)}</div>`:''}
            ${s.error?`<div class="quote small st-red" style="margin-top:6px">${esc(s.error)}</div>`:''}
          </div></div>`;
      }

      function play(session){
        if(timer) clearInterval(timer);
        const steps = Array.from(document.querySelectorAll('#rpSteps .pipe-step'));
        steps.forEach(el=>el.classList.remove('playing','played'));
        let i = 0;
        playBtn.disabled = true;
        timer = setInterval(()=>{
          if(i > 0 && steps[i-1]) steps[i-1].classList.replace('playing','played');
          if(i >= steps.length){
            clearInterval(timer); timer = null; playBtn.disabled = false;
            toast('success','Replay complete', `${session.step_count} steps replayed.`);
            return;
          }
          steps[i].classList.add('playing');
          steps[i].scrollIntoView({block:'nearest', behavior:'smooth'});
          i += 1;
        }, 620);
      }

      this.cleanup = ()=>{ if(timer) clearInterval(timer); };
    },
  };

  /* ================= METRICS ================= */
  SCREENS['metrics'] = {
    title:'Metrics',
    render(main){
      let window_ = '30d';
      // null means the whole workspace. A single id narrows every measurement
      // on the screen to one agent, which is the question people actually ask
      // once more than one agent is reporting.
      // An agent handed over from the Agent Registry preselects the filter.
      // Consumed on arrival so a later visit to Metrics is not stuck on it.
      let agentId = APP.metricsAgent || null;
      APP.metricsAgent = null;

      main.innerHTML = `
        ${pageHead({title:'Metrics', sub:'Execution, latency, token and cost trends — across the workspace or for one agent.',
          actions:`<select class="filter-select" id="mtAgent" style="height:34px;min-width:190px">
              <option value="">All agents</option>
            </select>
            <select class="filter-select" id="mtWindow" style="height:34px">
              ${[['24h','Last 24 hours'],['7d','Last 7 days'],['30d','Last 30 days'],['90d','Last 90 days']]
                .map(([v,l])=>`<option value="${v}" ${v===window_?'selected':''}>${l}</option>`).join('')}
            </select>
            <button class="btn" id="mtExport">${ICONS.download}Export</button>`})}
        <div id="mtKpis">${kpiSkeleton(['Total Runs','Success Rate','p50 Latency','p90 Latency','Tokens','Cost'])}</div>
        <div class="grid g2" id="mtCharts" style="margin-top:14px"></div>
        <div id="mtModels" style="margin-top:14px"></div>`;

      document.getElementById('mtWindow').addEventListener('change', e=>{ window_ = e.target.value; loadAll(); });
      document.getElementById('mtAgent').addEventListener('change', e=>{ agentId = e.target.value || null; loadAll(); });

      // Populate the agent picker. A failure here leaves "All agents" working,
      // because losing the filter is better than losing the screen.
      API.agents.list({ page_size: 100, sort: 'name' })
        .then(page => {
          const sel = document.getElementById('mtAgent');
          if(!sel) return;
          (page.items || []).forEach(a => {
            const o = document.createElement('option');
            o.value = a.id;
            o.textContent = a.name;
            if(a.id === agentId) o.selected = true;
            sel.appendChild(o);
          });
        })
        .catch(() => {});

      /** Query params for every metrics call on this screen. */
      function q(extra){ return Object.assign({ window: window_, agent_id: agentId || undefined }, extra || {}); }
      document.getElementById('mtExport').addEventListener('click', async () => {
        try { await API.metrics.export(q()); toast('success','Export complete','Metrics exported to CSV.'); }
        catch (err) { toast('error','Export failed', err.message); }
      });

      function loadAll(){ loadSummary(); loadCharts(); loadModels(); }

      function loadSummary(){
        const host = document.getElementById('mtKpis');
        if(!host) return;
        host.innerHTML = kpiSkeleton(['Total Runs','Success Rate','p50 Latency','p90 Latency','Tokens','Cost']);
        API.metrics.summary(q())
          .then(s => {
            if(!document.getElementById('mtKpis')) return;
            host.innerHTML = kpiRow([
              serverKpi(s.total_runs), serverKpi(s.success_rate), serverKpi(s.latency_p50),
              serverKpi(s.latency_p90), serverKpi(s.tokens), serverKpi(s.cost),
            ], 190);
          })
          .catch(err => { host.innerHTML=''; host.appendChild(screenError(err, loadSummary, 'the metric summary')); });
      }

      function loadCharts(){
        const host = document.getElementById('mtCharts');
        if(!host) return;
        host.innerHTML = `<div class="card"><div class="card-loading" style="height:220px"></div></div>
                          <div class="card"><div class="card-loading" style="height:220px"></div></div>`;
        Promise.all([
          API.metrics.series(q({ metric: 'runs' })),
          API.metrics.series(q({ metric: 'cost' })),
        ])
          .then(([runs, cost]) => {
            if(!document.getElementById('mtCharts')) return;
            host.innerHTML = `${chartCard('Runs over time', runs)}${chartCard('Cost over time', cost)}`;
          })
          .catch(err => { host.innerHTML=''; host.appendChild(screenError(err, loadCharts, 'the trend charts')); });
      }

      function chartCard(title, res){
        const s = (res.series || [])[0];
        if(!s || !s.points.length){
          return `<div class="card"><div class="card-head"><div class="card-title">${esc(title)}</div></div>
            <div class="empty-state">${ICONS.chart}<div class="es-title">No data in this window</div></div></div>`;
        }
        return `<div class="card"><div class="card-head"><div class="card-title">${esc(title)}</div>
            <div class="faint small">${esc(res.interval)}</div></div>
          <div style="padding:10px 4px">${lineChart({
            series: [{ name: s.label, color: s.color || 'purple', points: s.points.map(p => p.value == null ? 0 : p.value), area: true }],
            xLabels: s.points.map(p => p.label),
            zeroBase: true,
            w: 560, h: 210,
          })}</div></div>`;
      }

      function loadModels(){
        const host = document.getElementById('mtModels');
        if(!host) return;
        host.innerHTML = '';
        const table = dataTable({
          columns: [
            { key:'model', label:'Model', render:r=>`<b>${esc(r.model)}</b>` },
            { key:'agent_count', label:'Agents', align:'right', cls:'num', render:r=>fmtFull(r.agent_count) },
            { key:'runs', label:'Runs', align:'right', cls:'num', render:r=>fmtFull(r.runs) },
            { key:'tokens', label:'Tokens', align:'right', cls:'num', render:r=>r.tokens_display || dash },
            { key:'cost_usd', label:'Cost', align:'right', cls:'num', render:r=>r.cost_display || dash },
            { key:'avg_latency_seconds', label:'Avg Latency', align:'right', cls:'num', render:r=>secs(r.avg_latency_seconds) },
            { key:'success_rate_percent', label:'Success Rate', align:'right', cls:'num', render:r=>pct(r.success_rate_percent) },
            { key:'cost_share_percent', label:'Share of Cost', render:r=>r.cost_share_percent==null?dash:U.barPct(r.cost_share_percent,'orange', pct(r.cost_share_percent,0)) },
          ],
          rowId:'model', itemName:'models', pageSize:10, emptyText:'No model usage in this window',
          extraParams: q(),
          source: (params) => API.metrics.models(params),
        });
        host.appendChild(table.el);
      }

      loadAll();
    },
  };
})();
