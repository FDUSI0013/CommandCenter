/* Fulcrum Ops — QUALITY screens: Evaluations, Guardrails, Testing & Regression,
 * Feedback & Quality Loop.
 *
 * Every score, verdict, cluster and count on these screens is read from the
 * control plane, which reads the telemetry engine behind it. Nothing is scored
 * in the browser: when a judgement has not been made yet the cell shows "—",
 * and when the engine cannot answer the screen says so and offers a retry.
 */
(function(){
  'use strict';
  window.SCREENS = window.SCREENS || {};
  const { esc, relTime, fmtDateTime, fmtFull, lineChart, donut, gaugeRing, barPct, hbars, starRating } = U;
  const { badge, statusText, riskBadge, entityCell, ownerCell, kpiRow, kpiSkeleton, dataTable, pageHead, searchBox,
          tabBar, inspSection, kv, toast, openModal, confirmModal, screenError } = C;

  /* ---------------- null-safe formatters ----------------
   * A value the server did not send is a dash, never a zero. */
  const dash = '<span class="faint">—</span>';
  const num = (v) => v == null ? dash : fmtFull(v);
  const pct = (v, d) => v == null ? dash : Number(v).toFixed(d == null ? 1 : d) + '%';
  const ms = (v) => v == null ? dash : Math.round(v) + ' ms';
  const ts = (v) => v ? new Date(v).getTime() : null;
  const when = (v) => v ? `<span class="dim nowrap">${relTime(ts(v))}</span>` : dash;
  const dirOf = (v) => v == null || v === 0 ? null : (v > 0 ? 'up' : 'down');

  /**
   * A deadline, said as the time still left on it.
   *
   * `relTime` only speaks about the past — handed a future instant it answers
   * "just now", which would make an SLA with two days left look already blown.
   */
  function until(v){
    if(!v) return '—';
    const delta = ts(v) - Date.now();
    if(delta <= 0) return relTime(ts(v));
    const mins = Math.round(delta / 60000);
    if(mins < 60) return `in ${Math.max(1, mins)} min${mins === 1 ? '' : 's'}`;
    const hours = Math.round(delta / 3600000);
    if(hours < 48) return `in ${hours} hour${hours === 1 ? '' : 's'}`;
    const days = Math.round(delta / 86400000);
    return `in ${days} day${days === 1 ? '' : 's'}`;
  }

  /** A 0-1 judged score as the coloured pill the table uses. */
  function score(v){
    if(v == null) return dash;
    const cls = v >= 0.9 ? 'sc-good' : v >= 0.8 ? 'sc-mid' : 'sc-bad';
    return `<span class="score-pill ${cls}">${Number(v).toFixed(2)}</span>`;
  }

  /** A signed delta, coloured by whether the movement is welcome. */
  function delta(v, digits, goodWhenUp){
    if(v == null) return dash;
    const n = Number(v);
    if(n === 0) return '<span class="faint">±0.00</span>';
    const good = goodWhenUp === false ? n < 0 : n > 0;
    return `<span class="${good ? 'st-green' : 'st-red'}">${n > 0 ? '+' : '−'}${Math.abs(n).toFixed(digits == null ? 2 : digits)}</span>`;
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
   * A modal that watches a real background job.
   *
   * `poll` returns the server's progress record; the bar is drawn from the
   * counts it reports, never from a timer. Polling stops when the record says
   * it is terminal or when the modal closes.
   */
  function progressModal(cfg){
    let timer = null, closed = false;
    const handle = openModal({
      title: cfg.title, icon: cfg.icon || 'beaker',
      body: `<div id="pgBody"><div class="card-loading" style="height:96px"></div></div>`,
      footer: [{ label:'Run in Background', onClick:(close)=>{ close(); stop(); } }],
      onOpen(modal){ tick(modal); },
    });
    const overlay = handle.el.closest('.modal-overlay');
    const watcher = new MutationObserver(()=>{ if(!document.body.contains(handle.el)) stop(); });
    if(overlay && overlay.parentNode) watcher.observe(overlay.parentNode, { childList:true });

    function stop(){ closed = true; if(timer) clearTimeout(timer); timer = null; watcher.disconnect(); }

    function tick(modal){
      if(closed) return;
      cfg.poll()
        .then(p => {
          if(closed) return;
          const body = modal.querySelector('#pgBody');
          if(!body){ stop(); return; }
          body.innerHTML = paint(p);
          if(p.is_terminal){
            stop();
            if(cfg.onDone) cfg.onDone(p);
            setTimeout(()=>{ if(document.body.contains(modal)) handle.close(); }, 1400);
            return;
          }
          timer = setTimeout(()=>tick(modal), 1400);
        })
        .catch(err => {
          if(closed) return;
          stop();
          const body = modal.querySelector('#pgBody');
          if(body){ body.innerHTML = ''; body.appendChild(screenError(err, null, 'the progress of this run')); }
          if(cfg.onFail) cfg.onFail(err);
        });
    }

    // The poll outlives the render that started it, so the screen can end it.
    handle.stop = () => { stop(); handle.close(); };

    function paint(p){
      const done = p.percent == null ? 0 : Math.max(0, Math.min(100, p.percent));
      return `<div class="flex between" style="margin-bottom:6px">
          <b>${esc(p.phase || p.status || 'Working')}</b>
          <span class="num">${done.toFixed(0)}%</span></div>
        <div class="bar-bg" style="height:8px"><div class="bar-fill" style="width:${done}%;background:var(--purple)"></div></div>
        ${kv([
          ['Status', statusText(p.status)],
          ['Cases', `${fmtFull(p.processed_cases)} of ${fmtFull(p.total_cases)}`],
          ['Scored', num(p.scored_cases)],
          ...(p.passed != null ? [['Passed', num(p.passed)], ['Failed', num(p.failed)]] : []),
          ['Elapsed', p.elapsed_seconds == null ? dash : U.fmtDur(p.elapsed_seconds)],
          ...(p.detail ? [['Detail', `<span class="small dim right">${esc(p.detail)}</span>`]] : []),
        ])}`;
    }
    return handle;
  }

  /* ================= EVALUATIONS ================= */
  const EV_KPIS = ['Evaluations (30d)','Avg Score','Test Cases Run','Regressions Caught','Judge Model'];

  SCREENS['evaluations'] = {
    title:'Evaluations',
    render(main){
      let selectedId = null;
      // Progress polls outlive a render; the router closes them on navigation.
      const pollers = [];
      this.cleanup = () => { pollers.splice(0).forEach(p => { try { p.stop(); } catch (_) {} }); };

      main.innerHTML = `
        ${pageHead({title:'Evaluations', sub:'LLM quality evaluations across agents — correctness, grounding, faithfulness, and safety.',
          actions:`${searchBox('evSearch','Search evaluations…')}
          <button class="btn" id="evExport">${ICONS.download}Export</button>
          <button class="btn primary" id="evNew"${gate('member','Running an evaluation requires the member role.')}>${ICONS.play}Run Evaluation</button>`})}
        <div id="evKpis">${kpiSkeleton(EV_KPIS)}</div>
        <div id="evTrend" class="mt"></div>
        <div class="with-inspector mt" id="evLayout">
          <div id="evTableWrap"></div>
          <div class="inspector" id="evInspector"></div>
        </div>`;

      // ---- KPI cards ---------------------------------------------------
      function loadSummary(){
        const host = document.getElementById('evKpis');
        if(!host) return;
        host.innerHTML = kpiSkeleton(EV_KPIS);
        API.evaluations.summary()
          .then(s => {
            if(!document.getElementById('evKpis')) return;
            const w = `vs previous ${s.window_days} days`;
            host.innerHTML = kpiRow([
              { label:`Evaluations (${s.window_days}d)`, value:fmtFull(s.evaluations), icon:'beaker', color:'purple',
                delta: s.evaluations_delta ? Math.abs(s.evaluations_delta) + ' runs' : null,
                dir: dirOf(s.evaluations_delta), good: s.evaluations_delta >= 0, vs: w,
                sub: s.running ? `${s.running} running` : null },
              { label:'Avg Score', value: s.avg_score == null ? '—' : s.avg_score.toFixed(2), icon:'target', color:'green',
                delta: s.avg_score_delta == null ? null : Math.abs(s.avg_score_delta).toFixed(2),
                dir: dirOf(s.avg_score_delta), good: s.avg_score_delta >= 0, vs: w },
              { label:'Test Cases Run', value:fmtFull(s.cases_run), icon:'layers', color:'blue',
                delta: s.cases_run_delta_percent == null ? null : Math.abs(s.cases_run_delta_percent).toFixed(1) + '%',
                dir: dirOf(s.cases_run_delta_percent), good: s.cases_run_delta_percent >= 0, vs: w },
              { label:'Regressions Caught', value:fmtFull(s.regressions_caught), icon:'bug', color:'red',
                delta: s.regressions_caught_delta ? Math.abs(s.regressions_caught_delta) + ' runs' : null,
                dir: dirOf(s.regressions_caught_delta), good: s.regressions_caught_delta <= 0, vs: w,
                sub: s.failed ? `${s.failed} failed` : null },
              { label:'Judge Model', value:`<span style="font-size:16px">${esc(s.judge_model || '—')}</span>`,
                sub: s.judge_method, icon:'cpu', color:'cyan' },
            ]);
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSummary, 'the evaluation summary')); });
      }

      // ---- score trend --------------------------------------------------
      const TREND_SERIES = [
        ['avg_score','Average','purple'], ['correctness','Correctness','green'],
        ['grounding','Grounding','blue'], ['faithfulness','Faithfulness','cyan'], ['safety','Safety','amber'],
      ];

      function loadTrend(){
        const host = document.getElementById('evTrend');
        if(!host) return;
        host.innerHTML = `<div class="card"><div class="card-loading" style="height:200px"></div></div>`;
        API.evaluations.trend({ window_days: 30 })
          .then(t => {
            if(!document.getElementById('evTrend')) return;
            const points = (t.points || []).filter(p => p.avg_score != null);
            if(points.length < 2){
              host.innerHTML = `<div class="card"><div class="card-head"><div class="card-title">Score Trend</div>
                <div class="faint small">Last ${t.window_days} days</div></div>
                ${emptyBlock('chart','Not enough completed evaluations to plot a trend',
                  points.length ? 'One judged run so far — a second gives the chart a line to draw.' : 'Run an evaluation and its judged average appears here.')}</div>`;
              return;
            }
            // lineChart cannot draw a gap, and shifting a series' points onto
            // other days would misdate them — so a metric the judge did not
            // score on every plotted day stays off the chart entirely rather
            // than being plotted at an invented 0.
            const series = TREND_SERIES
              .filter(([key]) => points.every(p => p[key] != null))
              .map(([key, name, color]) => ({
                name, color, area: key === 'avg_score',
                points: points.map(p => p[key]),
              }));
            host.innerHTML = `<div class="card">
              <div class="card-head"><div class="card-title">Score Trend</div>
                <div class="faint small">Last ${t.window_days} days · ${points.length} judged days</div></div>
              ${lineChart({ series, h:200, min:0, max:1, yFmt:v=>v.toFixed(2),
                xLabels: points.map(p => p.date.slice(5)), maxXLabels:8 })}
              <div class="legend inline" style="margin-top:6px">${series.map(s =>
                `<span class="legend-item"><span class="sw" style="background:${U.cc(s.color)}"></span><span class="lg-label">${esc(s.name)}</span></span>`).join('')}</div>
            </div>`;
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadTrend, 'the score trend')); });
      }

      // ---- the evaluations table ----------------------------------------
      const table = dataTable({
        columns:[
          { key:'agent_name', label:'Agent', render:r => entityCell(r.agent_name || r.name || '—', r.agent_model || (r.agent_id ? null : 'Dataset-level run'), 'bot', 'purple') },
          { key:'dataset', label:'Dataset', render:r => `<span class="mono">${esc(r.dataset)}</span>` },
          { key:'judge_model', label:'Judge', render:r => r.judge_model ? `<span class="dim">${esc(r.judge_model)}</span>` : dash },
          { key:'cases', label:'Cases', align:'right', cls:'num', render:r => num(r.cases) },
          { key:'correctness', label:'Correctness', align:'right', sortable:false, render:r => score(r.correctness) },
          { key:'grounding', label:'Grounding', align:'right', sortable:false, render:r => score(r.grounding) },
          { key:'faithfulness', label:'Faithfulness', align:'right', sortable:false, render:r => score(r.faithfulness) },
          { key:'safety', label:'Safety', align:'right', sortable:false, render:r => score(r.safety) },
          { key:'baseline_delta', label:'Δ vs Baseline', align:'right', sortable:false, render:r =>
              r.baseline_run_id == null ? '<span class="faint">no baseline</span>'
                : `${delta(r.baseline_delta)}${r.is_regression ? ' ' + badge('Regression','red') : ''}` },
          { key:'status', label:'Status', render:r => statusText(r.status) },
          { key:'occurred_at', label:'Time', render:r => when(r.occurred_at) },
        ],
        rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'evaluations',
        searchPlaceholder:'Search agent, dataset or judge…',
        defaultSort:{ key:'occurred_at', dir:-1 },
        emptyText:'No evaluations have been run in this workspace yet',
        filters:[
          { key:'status', label:'Status', param:'status', options:['Queued','Running','Completed','Failed'], allLabel:'All Status' },
          { key:'dataset', label:'Dataset', param:'dataset', options:[], allLabel:'All Datasets' },
          { key:'judge_model', label:'Judge', param:'judge_model', options:[], allLabel:'All Judges' },
        ],
        source: (params) => API.evaluations.list(params),
        exportSource: (params) => API.evaluations.export(params),
        onLoad: (rows) => { loadSummary(); fillJudges(rows); },
        autoSelectFirst: true,
        onSelect: showEval,
        rowActions: r => [
          { label:'Re-run Evaluation', icon:'refresh', onClick:()=>rerun(r) },
          ...(r.agent_id ? [{ label:'View Agent', icon:'bot', onClick:()=>APP.go('agent/' + r.agent_id) }] : []),
          { label:'Compare to Baseline', icon:'git', onClick:()=>openCompare(r) },
          { sep:true },
          { label:'View Test Suites', icon:'box', onClick:()=>APP.go('testing') },
        ],
      });

      const wrap = document.getElementById('evTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);
      document.getElementById('evSearch').addEventListener('input', e => table.search(e.target.value));
      document.getElementById('evExport').addEventListener('click', () => table.export());

      /** The judge list is workspace-specific, so it grows from what came back. */
      function fillJudges(rows){
        const sel = table.filterEl && table.filterEl.querySelector('[data-fi="2"]');
        if(!sel) return;
        const have = new Set(Array.from(sel.options).map(o => o.value || o.textContent));
        (rows || []).forEach(r => {
          if(r.judge_model && !have.has(r.judge_model)){
            have.add(r.judge_model);
            const o = document.createElement('option');
            o.textContent = r.judge_model;
            sel.appendChild(o);
          }
        });
      }

      /** The dataset picker is the engine's, so the dropdown waits on it. */
      function fillDatasets(){
        const sel = table.filterEl && table.filterEl.querySelector('[data-fi="1"]');
        if(!sel) return;
        API.evaluations.datasets()
          .then(page => {
            (page.items || []).forEach(d => {
              const o = document.createElement('option');
              o.textContent = d.name;
              sel.appendChild(o);
            });
            if(!(page.items || []).length) sel.title = 'This workspace has no evaluation datasets yet.';
          })
          .catch(err => { sel.title = 'Datasets could not be listed: ' + ((err && err.message) || 'unavailable'); });
      }
      fillDatasets();
      loadTrend();

      // ---- inspector ------------------------------------------------------
      function showEval(row){
        const insp = document.getElementById('evInspector');
        if(!insp || !row) return;
        selectedId = row.id;
        document.getElementById('evLayout').classList.remove('collapsed');
        insp.innerHTML = `<div class="insp-head"><div class="grow">
            <div class="insp-title">${esc(row.agent_name || row.name || 'Evaluation')}</div>
            <div class="insp-sub">${esc(row.dataset)}</div></div>
            <button class="icon-btn insp-close" id="evClose">${ICONS.x}</button></div>
          <div class="card-loading" style="height:220px;margin:12px"></div>`;
        insp.querySelector('#evClose').addEventListener('click', ()=>document.getElementById('evLayout').classList.add('collapsed'));

        API.evaluations.detail(row.id, { item_page: 1, item_page_size: 8 })
          .then(d => { if(selectedId === row.id) paintEval(insp, d); })
          .catch(err => {
            if(selectedId !== row.id) return;
            const holder = insp.querySelector('.card-loading');
            if(holder) holder.replaceWith(screenError(err, ()=>showEval(row), 'this evaluation'));
          });
      }

      function paintEval(insp, d){
        const metrics = d.metrics || [];
        const trend = (d.trend || []).filter(p => p.avg_score != null);
        insp.innerHTML = `
          <div class="insp-head"><div class="grow">
            <div class="insp-title">${esc(d.agent_name || d.name || 'Evaluation')}</div>
            <div class="insp-sub">${esc(d.dataset)} · ${fmtFull(d.cases)} cases · ${d.occurred_at ? relTime(ts(d.occurred_at)) : '—'}</div></div>
            <button class="icon-btn insp-close" id="evClose">${ICONS.x}</button></div>
          ${inspSection('Overall Score','target', d.avg_score == null
            ? emptyBlock('target', d.status === 'Completed' ? 'The judge returned no average for this run' : 'Not scored yet',
                d.status === 'Running' ? 'Scores appear as the judge works through the cases.' : '')
            : `<div class="donut-wrap">${gaugeRing(d.avg_score * 100, 'purple', 110, 'Avg Score')}
              <div class="legend grow">${metrics.map(m =>
                `<div class="legend-item"><span class="sw" style="background:${m.value == null ? '#94A3B8' : m.value >= 0.9 ? '#16A34A' : '#D97706'}"></span>
                  <span class="lg-label">${esc(m.label)}</span>
                  <span class="lg-val">${m.value == null ? dash : m.value.toFixed(2)}${m.delta == null ? '' : `<br><span class="small">${delta(m.delta)}</span>`}</span></div>`).join('')
                || '<span class="faint small">The judge reported no per-metric scores.</span>'}
              </div></div>`)}
          ${inspSection('Run & Baseline','git', kv([
            ['Status', statusText(d.status)],
            ['Judge', d.judge_model ? esc(d.judge_model) : dash],
            ['Cases Scored', `${fmtFull(d.scored_items)} of ${fmtFull(d.item_total)}`],
            ['Duration', d.duration_seconds == null ? dash : U.fmtDur(d.duration_seconds)],
            ['Started', d.started_at ? fmtDateTime(ts(d.started_at)) : dash],
            ['Finished', d.finished_at ? fmtDateTime(ts(d.finished_at)) : dash],
            ['Baseline Avg', d.baseline_avg_score == null ? dash : d.baseline_avg_score.toFixed(2)],
            ['Δ vs Baseline', d.baseline_run_id == null ? '<span class="faint">no baseline yet</span>' : delta(d.baseline_delta)],
            ['Regression', d.is_regression ? badge('Yes','red') : badge('No','green')],
            ...(d.notes ? [['Notes', `<span class="small dim right">${esc(d.notes)}</span>`]] : []),
          ]))}
          ${inspSection('Score Trend','chart', trend.length > 1
            ? lineChart({ series:[{ color:'purple', points: trend.map(p=>p.avg_score), area:true, dots:true }],
                h:120, min:0, max:1, yFmt:v=>v.toFixed(2), xLabels: trend.map(p=>p.date.slice(5)), maxXLabels:6 })
            : '<div class="faint small">This agent and dataset need a second judged run before a trend can be drawn.</div>')}
          ${inspSection('Case Breakdown','layers', (d.items || []).length
            ? (d.items.map(it => `<div class="quote" style="border-color:${it.passed === false ? 'rgba(239,68,68,.4)' : 'var(--border)'}">
                <div class="flex between"><b>Case ${it.index}</b>
                  <span>${it.avg_score == null ? dash : score(it.avg_score)} ${it.passed == null ? badge('Unscored','gray') : badge(it.passed ? 'Passed' : 'Failed', it.passed ? 'green' : 'red')}</span></div>
                <div class="small dim" style="margin-top:4px">${esc((it.input || '').slice(0, 150) || '—')}</div></div>`).join('')
              + `<div class="small faint" style="margin-top:6px">Showing ${d.items.length} of ${fmtFull(d.item_total)} cases.</div>`)
            : '<div class="faint small">The engine has returned no cases for this run.</div>')}
          <div class="insp-section"><div class="grid g2" style="gap:8px">
            <button class="btn sm primary" id="evRerun"${gate('member','Re-running an evaluation requires the member role.')}>${ICONS.refresh}Re-run</button>
            <button class="btn sm" id="evCompare">${ICONS.git}Compare</button></div></div>`;
        insp.querySelector('#evClose').addEventListener('click', ()=>document.getElementById('evLayout').classList.add('collapsed'));
        insp.querySelector('#evRerun').addEventListener('click', ()=>rerun(d));
        insp.querySelector('#evCompare').addEventListener('click', ()=>openCompare(d));
      }

      // ---- actions ---------------------------------------------------------
      async function rerun(r){
        if(!allowed('member','Re-running an evaluation requires the member role.')) return;
        try {
          const started = await Store.mutate(() => API.evaluations.rerun(r.id), { event:'evaluations:changed' });
          toast('success','Evaluation queued', `${r.agent_name || r.dataset} · ${String(started.id).slice(0,12)}…`);
          table.refresh();
          watchEvaluation(started.id, r);
        } catch (err) {
          toast('error','Could not re-run', errText(err));
        }
      }

      function watchEvaluation(id, label){
        pollers.push(progressModal({
          title:'Evaluation Progress', icon:'beaker',
          poll: () => API.evaluations.progress(id),
          onDone: (p) => {
            table.refresh(); loadSummary(); loadTrend();
            toast(p.status === 'Failed' ? 'error' : 'success',
              p.status === 'Failed' ? 'Evaluation failed' : 'Evaluation complete',
              `${(label && (label.agent_name || label.dataset)) || 'Run'} — ${fmtFull(p.scored_cases)} of ${fmtFull(p.total_cases)} cases scored.`);
          },
          onFail: () => table.refresh(),
        }));
      }

      function openCompare(r){
        if(!r.baseline_run_id){
          toast('warn','No baseline yet', 'This is the first completed run for that agent and dataset, so there is nothing to compare it against.');
          return;
        }
        openModal({
          title:'Baseline Comparison', icon:'git', wide:true,
          body:`<div class="card-loading" style="height:180px"></div>`,
          footer:[{ label:'Close' }],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.evaluations.compare({ baseline: r.baseline_run_id, candidate: r.id })
              .then(c => {
                const vColor = c.verdict === 'Improved' ? 'green' : c.verdict === 'Regressed' ? 'red' : 'gray';
                body.innerHTML = `
                  <div class="flex between" style="margin-bottom:10px">
                    <div><b>${esc(c.candidate.agent_name || c.candidate.name)}</b>
                      <div class="small dim">${esc(c.candidate.dataset)} · candidate ${esc(String(c.candidate.id).slice(0,10))}… vs baseline ${esc(String(c.baseline.id).slice(0,10))}…</div></div>
                    ${badge(c.verdict, vColor)}</div>
                  <table class="tbl"><thead><tr><th>Metric</th><th class="right">Baseline</th><th class="right">Candidate</th><th class="right">Δ</th></tr></thead><tbody>
                    ${(c.metrics || []).map(m => `<tr style="cursor:default"><td class="cell-main">${esc(m.label)}</td>
                      <td class="right num">${m.baseline == null ? dash : m.baseline.toFixed(2)}</td>
                      <td class="right num">${m.value == null ? dash : m.value.toFixed(2)}</td>
                      <td class="right num">${delta(m.delta)}</td></tr>`).join('')
                      || `<tr><td colspan="4">${emptyBlock('target','Neither run carries per-metric scores','')}</td></tr>`}
                    <tr style="cursor:default"><td class="cell-main"><b>Average</b></td>
                      <td class="right num">${c.baseline.avg_score == null ? dash : c.baseline.avg_score.toFixed(2)}</td>
                      <td class="right num">${c.candidate.avg_score == null ? dash : c.candidate.avg_score.toFixed(2)}</td>
                      <td class="right num">${delta(c.avg_score_delta)}</td></tr>
                  </tbody></table>
                  ${(c.regressed_metrics || []).length ? `<div class="quote mt" style="border-color:rgba(239,68,68,.4)"><b style="color:#B91C1C">Regressed:</b> ${esc(c.regressed_metrics.join(', '))}</div>` : ''}
                  ${(c.improved_metrics || []).length ? `<div class="quote mt" style="border-color:rgba(22,163,74,.4)"><b style="color:#15803D">Improved:</b> ${esc(c.improved_metrics.join(', '))}</div>` : ''}`;
              })
              .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, null, 'the baseline comparison')); });
          },
        });
      }

      // ---- Run Evaluation --------------------------------------------------
      document.getElementById('evNew').addEventListener('click', async () => {
        if(!allowed('member','Running an evaluation requires the member role.')) return;
        let agents = { items: [] }, datasets = null, dsError = null;
        try { agents = await API.agents.list({ page_size: 100, status: 'Active' }); } catch (_) { /* the picker degrades to none */ }
        try { datasets = await API.evaluations.datasets(); } catch (err) { dsError = err; }

        const dsField = dsError
          ? `<div class="quote" style="border-color:rgba(239,68,68,.4)"><b style="color:#B91C1C">Datasets unavailable</b>
               <div class="small dim">${esc(dsError.message)}</div></div>`
          : (datasets.items || []).length
            ? `<select class="filter-select w-100" id="neDs" style="height:34px">${datasets.items.map(d =>
                `<option value="${esc(d.name)}">${esc(d.name)} — ${fmtFull(d.case_count)} cases</option>`).join('')}</select>`
            : `<div class="quote"><b>No datasets yet</b><div class="small dim">This workspace has no evaluation datasets to run against.</div></div>`;
        const canStart = !dsError && (datasets.items || []).length;

        openModal({
          title:'Run Evaluation', icon:'beaker',
          body:`<div class="form-row"><label>AGENT</label>
              <select class="filter-select w-100" id="neAgent" style="height:34px">
                <option value="">Dataset-level run (no agent)</option>
                ${(agents.items || []).map(a => `<option value="${esc(a.id)}">${esc(a.name)}${a.environment ? ' — ' + esc(a.environment) : ''}</option>`).join('')}
              </select>
              ${(agents.items || []).length ? '' : '<div class="small faint" style="margin-top:4px">No active agents are registered, so only a dataset-level run is possible.</div>'}</div>
            <div class="form-row"><label>DATASET</label>${dsField}</div>
            <div class="form-row"><label>JUDGE MODEL</label><input class="input" id="neJudge" value="gpt-4o" placeholder="e.g. gpt-4o"></div>
            <div class="form-row"><label>NAME (OPTIONAL)</label><input class="input" id="neName" placeholder="e.g. Nightly invoice check"></div>`,
          footer:[
            { label:'Cancel' },
            { label:'Start Evaluation', cls:'primary', onClick: async (close, modal) => {
                const dsEl = modal.querySelector('#neDs');
                if(!dsEl){ toast('error','Cannot start','No dataset is available to evaluate against.'); return; }
                const agentId = modal.querySelector('#neAgent').value;
                const body = {
                  dataset: dsEl.value,
                  judge_model: modal.querySelector('#neJudge').value.trim() || 'gpt-4o',
                };
                if(agentId) body.agent_id = agentId;
                const name = modal.querySelector('#neName').value.trim();
                if(name) body.name = name;
                close();
                try {
                  const started = await Store.mutate(() => API.evaluations.run(body), { event:'evaluations:changed' });
                  toast('success','Evaluation started', `${esc(started.dataset)} · ${String(started.id).slice(0,12)}…`);
                  table.refresh();
                  watchEvaluation(started.id, started);
                } catch (err) {
                  toast('error','Could not start the evaluation', errText(err));
                }
              } },
          ],
          onOpen(modal){
            if(!canStart){
              const btn = modal.querySelector('[data-mbtn="1"]');
              if(btn){ btn.disabled = true; btn.title = 'There is no dataset to evaluate against.'; }
            }
          },
        });
      });
    },
  };

  /* ================= GUARDRAILS ================= */
  const GR_KPIS = ['Active Guardrails','Triggers (30d)','Blocked (30d)','PII Items Masked (30d)','Avg Added Latency'];
  const GR_TYPES = ['Prompt Injection','PII','Toxicity','Hallucination','Secrets','Topic','Custom'];
  const GR_ACTIONS = ['Block','Mask','Warn','Log'];
  const GR_STATUS = ['Active','Disabled','Tuning'];
  const GR_SCOPES = ['Global','Agent','Environment'];
  const GR_STATUS_LABEL = {
    'Active':'Active — enforce the action',
    'Tuning':'Tuning — record detections, enforce nothing',
    'Disabled':'Disabled — do not check',
  };
  // The inline checker on this deployment implements only these three
  // validations; any other type is recorded but cannot block or mask at ingest.
  const GR_ENFORCEABLE = ['PII','Topic','Prompt Injection'];
  // `config` is what the content checker is told to look for, and these are the
  // keys each type's form collects. A PATCH replaces the stored config whole, so
  // a key no form owns is carried through an edit untouched.
  const GR_CONFIG_KEYS = { 'Topic':['topics','mode'], 'PII':['entities','language'], 'Secrets':['patterns'], 'Custom':['validation','patterns'] };
  // "Active" is a setting; whether live content is actually checked is a
  // separate fact the server reports as `enforcement`. These three read Active
  // and enforce nothing.
  const GR_NOT_ENFORCED = ['unsupported','misconfigured','suspended'];

  function grNotEnforced(r){
    if(r.enforcement) return GR_NOT_ENFORCED.includes(r.enforcement);
    return r.status === 'Active' && r.checker_supported === false;
  }
  function grReason(r){
    return r.not_enforced_reason
      || "This deployment's content checker does not implement this validation, so the rule is recorded but never enforced at ingest.";
  }
  function grStatusColor(r){
    return grNotEnforced(r) ? 'red' : r.status === 'Active' ? 'green' : r.status === 'Tuning' ? 'amber' : 'gray';
  }
  /** The status as the server set it, plus the warning when it enforces nothing. */
  function grStatusCell(r){
    return statusText(r.status, r.status === 'Active' ? 'green' : r.status === 'Tuning' ? 'amber' : 'gray')
      + (grNotEnforced(r) ? ` <span title="${esc(grReason(r))}">${badge('Not enforced','red')}</span>` : '');
  }

  /** The type-specific inputs for what the checker is told to look for. */
  function grConfigFields(type, config){
    const c = config || {};
    const list = (v) => Array.isArray(v) ? v.join('\n') : '';
    if(type === 'Topic') return `
      <div class="form-row"><label>TOPICS (ONE PER LINE)</label>
        <textarea class="input" id="gcTopics" rows="3" placeholder="e.g. investment advice">${esc(list(c.topics))}</textarea>
        <div class="small faint" style="margin-top:4px">Required: a Topic guardrail with no topics is saved but never enforced.</div></div>
      <div class="form-row"><label>MODE</label><select class="filter-select w-100" id="gcMode" style="height:34px">
        <option value="restrict" ${c.mode === 'allow' ? '' : 'selected'}>Restrict — trigger on content about these topics</option>
        <option value="allow" ${c.mode === 'allow' ? 'selected' : ''}>Allow — trigger on content about anything else</option></select></div>`;
    if(type === 'PII') return `
      <div class="grid g2">
        <div class="form-row"><label>ENTITIES (OPTIONAL)</label>
          <input class="input" id="gcEntities" value="${esc((Array.isArray(c.entities) ? c.entities : []).join(', '))}" placeholder="Comma-separated entity labels"></div>
        <div class="form-row"><label>LANGUAGE (OPTIONAL)</label>
          <input class="input" id="gcLang" value="${esc(typeof c.language === 'string' ? c.language : '')}" placeholder="e.g. en"></div>
      </div>
      <div class="small faint" style="margin:-2px 0 8px">Left blank, the checker uses its own entity list and language.</div>`;
    if(GR_CONFIG_KEYS[type]){
      const p = c.patterns && typeof c.patterns === 'object' ? c.patterns : {};
      return `${type === 'Custom' ? `<div class="form-row"><label>VALIDATOR NAME (OPTIONAL)</label>
          <input class="input" id="gcValidation" value="${esc(typeof c.validation === 'string' ? c.validation : '')}" placeholder="A bespoke validator the checker runs under this name"></div>` : ''}
        <div class="form-row"><label>PATTERNS (OPTIONAL — ONE PER LINE, LABEL = REGULAR EXPRESSION)</label>
          <textarea class="input mono" id="gcPatterns" rows="3" placeholder="INVOICE_NO = INV-[0-9]{6}">${esc(Object.keys(p).map(k => `${k} = ${p[k]}`).join('\n'))}</textarea></div>`;
    }
    return '';
  }

  /**
   * The config the form describes, or null after saying what is wrong with it.
   *
   * `stored` is the config being edited: every key this type's form does not
   * own rides along, because the server replaces the config with what is sent.
   */
  function grReadConfig(modal, type, stored, storedType){
    const out = Object.assign({}, stored || {});
    // The checker is sent every key it finds, so what the previous type's form
    // collected must not follow the guardrail into a type it means nothing to.
    if(storedType && storedType !== type) (GR_CONFIG_KEYS[storedType] || []).forEach(k => { delete out[k]; });
    const val = (sel) => { const el = modal.querySelector(sel); return el ? el.value : ''; };
    const names = (text) => text.split(/[\n,]/).map(x => x.trim()).filter(Boolean);
    const put = (key, value, keep) => { if(keep) out[key] = value; else delete out[key]; };

    if(type === 'Topic'){
      const topics = names(val('#gcTopics'));
      if(!topics.length && !out.validation){
        toast('error','Topics required','A Topic guardrail checks for the topics it is given. Add at least one.');
        return null;
      }
      put('topics', topics, topics.length > 0);
      out.mode = val('#gcMode') === 'allow' ? 'allow' : 'restrict';
    } else if(type === 'PII'){
      const entities = names(val('#gcEntities'));
      put('entities', entities, entities.length > 0);
      const lang = val('#gcLang').trim();
      put('language', lang, Boolean(lang));
    } else if(GR_CONFIG_KEYS[type]){
      if(type === 'Custom'){
        const validation = val('#gcValidation').trim();
        put('validation', validation, Boolean(validation));
      }
      const patterns = {};
      const lines = val('#gcPatterns').split('\n');
      for(let i = 0; i < lines.length; i++){
        const line = lines[i].trim();
        if(!line) continue;
        const cut = line.indexOf('=');
        const label = cut < 0 ? '' : line.slice(0, cut).trim();
        const expr = cut < 0 ? '' : line.slice(cut + 1).trim();
        if(!label || !expr){
          toast('error','Pattern not understood', `Line ${i + 1}: write each pattern as LABEL = regular expression.`);
          return null;
        }
        patterns[label] = expr;
      }
      put('patterns', patterns, Object.keys(patterns).length > 0);
    }
    return out;
  }

  SCREENS['guardrails'] = {
    title:'Guardrails',
    render(main){
      // `painted` is the row the inspector was last drawn from, as text, so a
      // table reload can tell whether the selected guardrail has moved under it.
      let selectedId = null, painted = null;

      main.innerHTML = `
        ${pageHead({title:'Guardrails', sub:'Runtime safety filters for prompts and responses — injection, PII, toxicity, hallucination, and secrets.',
          actions:`${searchBox('grSearch','Search guardrails…')}
          <button class="btn" id="grExport">${ICONS.download}Export</button>
          <button class="btn primary" id="grNew"${gate('admin','Creating a guardrail requires the admin role.')}>${ICONS.plus}New Guardrail</button>`})}
        <div id="grKpis">${kpiSkeleton(GR_KPIS)}</div>
        <div class="with-inspector mt" id="grLayout">
          <div id="grTableWrap"></div>
          <div class="inspector" id="grInspector"></div>
        </div>`;

      function loadSummary(){
        const host = document.getElementById('grKpis');
        if(!host) return;
        host.innerHTML = kpiSkeleton(GR_KPIS);
        API.guardrails.summary()
          .then(s => {
            if(!document.getElementById('grKpis')) return;
            const w = `vs previous ${s.window_days} days`;
            // An Active guardrail the checker cannot run protects nothing, so
            // the card that counts them says how many of them that is.
            const suspended = s.suspended_validations || [];
            host.innerHTML = kpiRow([
              { label:'Active Guardrails', value:fmtFull(s.active), icon:'shieldCheck', color: s.not_enforced ? 'red' : 'purple',
                sub:`of ${fmtFull(s.configured)} configured · ${fmtFull(s.disabled)} disabled, ${fmtFull(s.tuning)} tuning`
                  + (s.not_enforced ? ` · ${fmtFull(s.not_enforced)} not enforced` : '')
                  + (suspended.length ? ` · suspended: ${suspended.join(', ')}` : '') },
              { label:`Triggers (${s.window_days}d)`, value:fmtFull(s.triggers), icon:'zap', color:'amber',
                delta: s.triggers_delta_percent == null ? null : Math.abs(s.triggers_delta_percent).toFixed(1) + '%',
                dir: dirOf(s.triggers_delta_percent), good: s.triggers_delta_percent <= 0, vs: w },
              { label:`Blocked (${s.window_days}d)`, value:fmtFull(s.blocked), icon:'xCircle', color:'red',
                sub: s.last_triggered_at ? 'Last trigger ' + relTime(ts(s.last_triggered_at)) : 'No triggers recorded' },
              { label:`PII Items Masked (${s.window_days}d)`, value:fmtFull(s.pii_items_masked), icon:'eyeOff', color:'blue',
                delta: s.pii_items_masked_delta_percent == null ? null : Math.abs(s.pii_items_masked_delta_percent).toFixed(1) + '%',
                dir: dirOf(s.pii_items_masked_delta_percent), good: s.pii_items_masked_delta_percent >= 0, vs: w },
              { label:'Avg Added Latency', value: s.avg_added_latency_ms == null ? '—' : Math.round(s.avg_added_latency_ms) + 'ms',
                sub:'Per guarded call', icon:'clock', color:'green' },
            ]);
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSummary, 'the guardrail summary')); });
      }

      const table = dataTable({
        columns:[
          { key:'name', label:'Guardrail', render:r => entityCell(r.name, r.coverage, 'shieldCheck', grStatusColor(r)) },
          { key:'guardrail_type', label:'Type', render:r => badge(r.guardrail_type, 'purple') },
          { key:'status', label:'Status', render:grStatusCell },
          { key:'action', label:'Action', render:r => badge(r.action, r.action === 'Block' ? 'red' : r.action === 'Mask' ? 'amber' : 'gray') },
          { key:'triggers_30d', label:'Triggers (30d)', align:'right', cls:'num', render:r => num(r.triggers_30d) },
          { key:'blocked_30d', label:'Blocked (30d)', align:'right', cls:'num', render:r => num(r.blocked_30d) },
          { key:'effectiveness', label:'Effectiveness', render:r => r.effectiveness == null ? dash : barPct(r.effectiveness, 'green', pct(r.effectiveness)) },
          { key:'last_triggered_at', label:'Last Triggered', render:r => when(r.last_triggered_at) },
        ],
        rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'guardrails',
        searchPlaceholder:'Search guardrails…',
        defaultSort:{ key:'name', dir:1 },
        emptyText:'No guardrails are configured in this workspace yet',
        filters:[
          { key:'status', label:'Status', param:'status', options:GR_STATUS, allLabel:'All Status' },
          { key:'guardrail_type', label:'Type', param:'type', options:GR_TYPES, allLabel:'All Types' },
          { key:'action', label:'Action', param:'action', options:GR_ACTIONS, allLabel:'All Actions' },
          { key:'scope', label:'Scope', param:'scope', options:GR_SCOPES, allLabel:'All Scopes' },
        ],
        source: (params) => API.guardrails.list(params),
        exportSource: (params) => API.guardrails.export(params),
        // The KPI cards do not depend on the table's page, sort, filter or
        // search, and each summary is a heavy read server-side — so it loads
        // once with the screen and again after a change, not on every reload.
        onLoad: resync,
        autoSelectFirst: true,
        onSelect: (row) => showGuardrail(row),
        rowActions: r => [
          { label:'Test Guardrail', icon:'beaker', onClick:()=>openTest(r) },
          { label:'Tune Threshold', icon:'sliders', onClick:()=>openTune(r) },
          { label:'Edit Guardrail', icon:'edit', onClick:()=>openEdit(r) },
          { sep:true },
          ...(r.status !== 'Active' ? [{ label:'Enable', icon:'checkCircle', onClick:()=>setStatus(r, 'Active') }] : []),
          ...(r.status !== 'Tuning' ? [{ label:'Run in Tuning', icon:'eye', onClick:()=>setStatus(r, 'Tuning') }] : []),
          ...(r.status !== 'Disabled' ? [{ label:'Disable', icon:'xCircle', danger:true, onClick:()=>setStatus(r, 'Disabled') }] : []),
          { sep:true },
          { label:'Delete Guardrail', icon:'trash', danger:true, onClick:()=>remove(r) },
        ],
      });

      const wrap = document.getElementById('grTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);
      document.getElementById('grSearch').addEventListener('input', e => table.search(e.target.value));
      document.getElementById('grExport').addEventListener('click', () => table.export());
      loadSummary();

      // ---- inspector -------------------------------------------------------
      /** What the checker is told to look for, as the rows of the config card. */
      function configRows(row){
        const c = row.config || {};
        const tags = (v) => v.map(x => `<span class="tag">${esc(x)}</span>`).join('');
        const rows = [];
        if(row.guardrail_type === 'Topic'){
          rows.push(['Topics', Array.isArray(c.topics) && c.topics.length ? tags(c.topics)
            : `<span class="${c.validation ? 'faint' : 'st-red'}">none configured</span>`]);
          rows.push(['Mode', c.mode === 'allow' ? 'Allow — only these topics' : 'Restrict — these topics']);
        }
        if(Array.isArray(c.entities) && c.entities.length) rows.push(['Entities', tags(c.entities)]);
        if(typeof c.language === 'string' && c.language) rows.push(['Language', esc(c.language)]);
        if(typeof c.validation === 'string' && c.validation) rows.push(['Validator', `<span class="mono">${esc(c.validation)}</span>`]);
        if(c.patterns && typeof c.patterns === 'object' && Object.keys(c.patterns).length) rows.push(['Patterns', tags(Object.keys(c.patterns))]);
        return rows;
      }

      function closeInspector(){
        selectedId = null; painted = null;
        const layout = document.getElementById('grLayout'), insp = document.getElementById('grInspector');
        if(layout) layout.classList.add('collapsed');
        if(insp) insp.innerHTML = '';
      }

      /**
       * Redraw the inspector when the table reloads with a newer copy of the
       * guardrail it shows.
       *
       * The table replaces its rows on every refresh but only calls onSelect on
       * a click, so after Enable, Disable, Tune or Edit the inspector kept the
       * old status, threshold and action — and its Tune button opened on them
       * and posted them back. A panel the reader closed stays closed.
       */
      function resync(rows){
        if(selectedId == null) return;
        const cur = (rows || []).find(x => x.id === selectedId);
        if(cur && JSON.stringify(cur) !== painted) showGuardrail(cur, true);
      }

      function showGuardrail(row, keepClosed){
        const insp = document.getElementById('grInspector');
        if(!insp || !row) return;
        selectedId = row.id;
        painted = JSON.stringify(row);
        if(!keepClosed) document.getElementById('grLayout').classList.remove('collapsed');
        insp.innerHTML = `
          <div class="insp-head"><div class="grow">
            <div class="insp-title">${esc(row.name)} ${grStatusCell(row)}</div>
            <div class="insp-sub">${esc(row.guardrail_type)} · ${esc(row.coverage)}</div></div>
            <button class="icon-btn insp-close" id="grClose">${ICONS.x}</button></div>
          ${grNotEnforced(row) ? `<div class="insp-section"><div class="quote" style="border-color:rgba(239,68,68,.4)">
            <b style="color:#B91C1C">Not enforced</b><div class="small dim" style="margin-top:3px">${esc(grReason(row))}</div></div></div>` : ''}
          ${inspSection('Configuration','settings', kv([
            ['Action', badge(row.action, row.action === 'Block' ? 'red' : row.action === 'Mask' ? 'amber' : 'gray')],
            ['Scope', esc(row.scope) + (row.scope_ref ? ` · <span class="mono">${esc(row.scope_ref)}</span>` : '')],
            ['Coverage', esc(row.coverage)],
            ['Threshold', row.threshold == null ? dash : Number(row.threshold).toFixed(2) + ' (confidence)'],
            ...configRows(row),
            ['Added Latency', ms(row.added_latency_ms)],
            ['Owner', row.owner_name ? esc(row.owner_name) : dash],
            ['Updated', row.updated_at ? fmtDateTime(ts(row.updated_at)) : dash],
          ]) + (row.enforcement === 'shadow' && row.not_enforced_reason
            ? `<div class="small faint" style="margin-top:8px">${esc(row.not_enforced_reason)}</div>` : ''))}
          ${inspSection('Activity (30d)','chart', `<div class="grid g2" style="gap:8px">
            ${[['Triggers', num(row.triggers_30d)], ['Blocked', num(row.blocked_30d)],
               ['Masked', num(row.masked_30d)],
               ['Effectiveness', row.effectiveness == null ? dash : pct(row.effectiveness)]].map(x =>
              `<div style="background:var(--panel-2);border:1px solid var(--border-soft);border-radius:8px;padding:8px">
                <div class="small faint">${x[0]}</div><div style="font-weight:700;margin-top:2px">${x[1]}</div></div>`).join('')}
          </div>`)}
          <div class="insp-section"><div class="insp-section-title">${ICONS.flag}Recent Detections
            <button class="link" id="grEvExport" style="margin-left:auto;font-size:11px">Export</button></div>
            <div id="grEvents"><div class="card-loading" style="height:90px"></div></div></div>
          <div class="insp-section"><div class="grid g2" style="gap:8px">
            <button class="btn sm primary" id="grTest"${gate('operator','Testing a guardrail requires the operator role.')}>${ICONS.beaker}Test</button>
            <button class="btn sm" id="grTune"${gate('admin','Tuning a guardrail requires the admin role.')}>${ICONS.sliders}Tune</button>
            <button class="btn sm" id="grEdit"${gate('admin','Editing a guardrail requires the admin role.')}>${ICONS.edit}Edit</button>
            <button class="btn sm danger" id="grDelete"${gate('admin','Deleting a guardrail requires the admin role.')}>${ICONS.trash}Delete</button></div></div>`;
        insp.querySelector('#grClose').addEventListener('click', ()=>document.getElementById('grLayout').classList.add('collapsed'));
        insp.querySelector('#grTest').addEventListener('click', ()=>openTest(row));
        insp.querySelector('#grTune').addEventListener('click', ()=>openTune(row));
        insp.querySelector('#grEdit').addEventListener('click', ()=>openEdit(row));
        insp.querySelector('#grDelete').addEventListener('click', ()=>remove(row));
        insp.querySelector('#grEvExport').addEventListener('click', async () => {
          try { await API.guardrails.eventsExport({ guardrail_id: row.id }); toast('success','Export complete','Detections exported to CSV.'); }
          catch (err) { toast('error','Export failed', errText(err)); }
        });
        loadEvents(row);
      }

      /** The detections feed, scoped to the selected guardrail. */
      function loadEvents(row){
        const host = document.getElementById('grEvents');
        if(!host) return;
        host.innerHTML = '<div class="card-loading" style="height:90px"></div>';
        API.guardrails.events({ guardrail_id: row.id, page_size: 6, sort: '-occurred_at' })
          .then(page => {
            if(selectedId !== row.id) return;
            const el = document.getElementById('grEvents');
            if(!el) return;
            if(!page.items.length){
              el.innerHTML = '<div class="faint small">No detections recorded for this guardrail.</div>';
              return;
            }
            el.innerHTML = page.items.map(e => `<div class="kv">
                <span class="k" style="color:var(--text)">${esc(e.agent_name || 'Unattributed run')}${e.match_count ? ` <span class="faint">· ${e.match_count} match${e.match_count === 1 ? '' : 'es'}</span>` : ''}</span>
                <span class="v small">${badge(e.action_taken, e.action_taken === 'Block' ? 'red' : e.action_taken === 'Mask' ? 'amber' : 'gray')}
                  <span class="faint">${relTime(ts(e.occurred_at))}</span></span></div>`).join('')
              + (page.total > page.items.length ? `<div class="small faint" style="margin-top:6px">Showing ${page.items.length} of ${fmtFull(page.total)} detections.</div>` : '');
          })
          .catch(err => {
            if(selectedId !== row.id) return;
            const el = document.getElementById('grEvents');
            if(el){ el.innerHTML = ''; el.appendChild(screenError(err, ()=>loadEvents(row), 'the detections feed')); }
          });
      }

      // ---- actions ---------------------------------------------------------
      /** Move a guardrail between Active, Tuning (shadow) and Disabled. */
      async function setStatus(r, to){
        if(!allowed('operator','Changing a guardrail\'s status requires the operator role.')) return;
        const verb = to === 'Active' ? API.guardrails.enable : to === 'Tuning' ? API.guardrails.shadow : API.guardrails.disable;
        try {
          const res = await Store.mutate(() => verb(r.id), { event:'guardrails:changed' });
          const state = (res.data && res.data.enforcement) || null;
          // Enable can succeed and still protect nothing; the person who just
          // pressed it is the one who needs to hear that, so it is not a success.
          if(to === 'Active' && GR_NOT_ENFORCED.includes(state)) toast('warn','Enabled, but not enforced', res.message || r.name, 8000);
          else if(to === 'Active') toast('success','Guardrail enabled', res.message || r.name);
          else if(to === 'Tuning') toast('info','Guardrail is tuning', res.message || r.name);
          else toast('warn','Guardrail disabled', res.message || r.name);
          table.refresh();
          loadSummary();
        } catch (err) {
          toast('error', to === 'Active' ? 'Could not enable' : to === 'Tuning' ? 'Could not start tuning' : 'Could not disable', errText(err));
        }
      }

      function openTest(r){
        if(!allowed('operator','Testing a guardrail requires the operator role.')) return;
        openModal({
          title:'Test — ' + r.name, icon:'beaker', wide:true,
          body:`<div class="form-row"><label>SAMPLE INPUT</label>
              <textarea class="input" id="tgInput" rows="3" placeholder="Paste a prompt or a response to check…"></textarea></div>
            <div id="tgResult"><div class="faint small">The check runs against the live content checker and reports what it actually found.</div></div>`,
          footer:[
            { label:'Close' },
            { label:'Run Test', cls:'primary', close:false, onClick: async (close, modal) => {
                const input = modal.querySelector('#tgInput').value;
                const res = modal.querySelector('#tgResult');
                if(!input.trim()){ res.innerHTML = '<div class="small st-red">Enter a sample to check.</div>'; return; }
                res.innerHTML = '<div class="card-loading" style="height:80px"></div>';
                try {
                  const v = await API.guardrails.test(r.id, { input });
                  res.innerHTML = `<div class="quote" style="border-color:${v.triggered ? 'rgba(239,68,68,.4)' : 'rgba(22,163,74,.4)'}">
                      <div class="flex between"><b>Verdict</b>${badge(v.verdict, v.triggered ? 'red' : 'green')}</div>
                      ${kv([
                        ['Score', v.score == null ? dash : Number(v.score).toFixed(3)],
                        ['Threshold', Number(v.threshold).toFixed(2)],
                        ['Action', badge(v.action, v.action === 'Block' ? 'red' : v.action === 'Mask' ? 'amber' : 'gray')
                          + (v.action_applied ? '' : ' <span class="faint small">not enforced while tuning</span>')],
                        ['Added Latency', ms(v.added_latency_ms)],
                        ['Matches', String((v.matched || []).length)],
                      ])}
                      <div class="small dim" style="margin-top:6px">${esc(v.detail || '')}</div></div>
                    ${(v.matched || []).length ? `<div class="mt"><div class="small muted" style="font-weight:700;margin-bottom:4px">MATCHED SPANS</div>
                      <table class="tbl"><thead><tr><th>Label</th><th>Excerpt</th><th class="right">Position</th><th class="right">Score</th></tr></thead><tbody>
                      ${v.matched.map(m => `<tr style="cursor:default"><td class="cell-main">${esc(m.label)}</td>
                        <td class="mono">${esc(m.text == null ? '—' : m.text)}</td>
                        <td class="right num">${m.start == null ? '—' : m.start + '–' + m.end}</td>
                        <td class="right num">${m.score == null ? '—' : Number(m.score).toFixed(2)}</td></tr>`).join('')}
                      </tbody></table></div>` : '<div class="small faint mt">The checker matched nothing in this sample.</div>'}
                    ${v.masked_text ? `<div class="mt"><div class="small muted" style="font-weight:700;margin-bottom:4px">MASKED OUTPUT</div>
                      <div class="quote" style="white-space:pre-wrap">${esc(v.masked_text)}</div></div>` : ''}`;
                } catch (err) {
                  res.innerHTML = '';
                  res.appendChild(screenError(err, null, 'the guardrail test'));
                }
              } },
          ],
        });
      }

      function openTune(r){
        if(!allowed('admin','Tuning a guardrail requires the admin role.')) return;
        openModal({
          title:'Tune Threshold — ' + r.name, icon:'sliders',
          body:`<div class="quote">Current threshold <b>${Number(r.threshold).toFixed(2)}</b> · action <b>${esc(r.action)}</b>.
              Raising the threshold makes the guardrail fire less often.</div>
            <div class="form-row mt"><label>THRESHOLD (0–1)</label>
              <input class="input" id="tuVal" type="number" min="0" max="1" step="0.01" value="${Number(r.threshold).toFixed(2)}"></div>
            <div class="form-row"><label>ACTION ON TRIGGER</label>
              <select class="filter-select w-100" id="tuAction" style="height:34px">${GR_ACTIONS.map(a =>
                `<option ${a === r.action ? 'selected' : ''}>${a}</option>`).join('')}</select></div>
            <div class="form-row"><label>REASON (RECORDED IN THE AUDIT TRAIL)</label>
              <input class="input" id="tuReason" placeholder="e.g. too many false positives on invoice numbers"></div>`,
          footer:[
            { label:'Cancel' },
            { label:'Save Threshold', cls:'primary', onClick: async (close, modal) => {
                // min/max on a number input do not stop typed values, so
                // check before the request rather than bouncing off a 422.
                const threshold = parseFloat(modal.querySelector('#tuVal').value);
                if(Number.isNaN(threshold) || threshold < 0 || threshold > 1){
                  toast('error','Threshold out of range','Enter a threshold between 0 and 1.'); return;
                }
                // The server applies any action it is sent, so one the admin
                // did not touch stays out of the request: a form opened on an
                // older copy of the row must not put its old action back.
                const body = { threshold };
                const action = modal.querySelector('#tuAction').value;
                if(action !== r.action) body.action = action;
                const reason = modal.querySelector('#tuReason').value.trim();
                if(reason) body.reason = reason;
                close();
                try {
                  const updated = await Store.mutate(() => API.guardrails.tune(r.id, body), { event:'guardrails:changed' });
                  toast('success','Threshold updated', `${updated.name} now fires at ${Number(updated.threshold).toFixed(2)} and applies ${updated.action}.`);
                  table.refresh();
                  loadSummary();
                } catch (err) {
                  toast('error','Could not tune the guardrail', errText(err));
                }
              } },
          ],
        });
      }

      // ---- create, edit, delete ---------------------------------------------
      /** The form New Guardrail and Edit Guardrail share; `r` is the row being edited. */
      function guardrailForm(r){
        const v = r || { name:'', guardrail_type:GR_TYPES[0], action:GR_ACTIONS[0], status:'Active', scope:'Global', scope_ref:'' };
        const opts = (list, sel, label) => list.map(x =>
          `<option value="${esc(x)}" ${x === sel ? 'selected' : ''}>${esc(label ? label(x) : x)}</option>`).join('');
        return `<div class="form-row"><label>NAME</label><input class="input" id="gfName" value="${esc(v.name)}" placeholder="e.g. Regulated Advice Filter"></div>
          <div class="grid g2">
            <div class="form-row"><label>TYPE</label><select class="filter-select w-100" id="gfType" style="height:34px">${opts(GR_TYPES, v.guardrail_type, t => t + (GR_ENFORCEABLE.includes(t) ? '' : ' — recorded only'))}</select></div>
            <div class="form-row"><label>ACTION</label><select class="filter-select w-100" id="gfAction" style="height:34px">${opts(GR_ACTIONS, v.action)}</select></div>
          </div>
          <div class="small faint" id="gfTypeHint" style="margin:-2px 0 8px"></div>
          <div id="gfConfig"></div>
          <div class="grid g2">
            <div class="form-row"><label>SCOPE</label><select class="filter-select w-100" id="gfScope" style="height:34px">${opts(GR_SCOPES, v.scope)}</select></div>
            <div class="form-row"><label>THRESHOLD (0–1)</label><input class="input" id="gfThreshold" type="number" min="0" max="1" step="0.01" value="${r ? esc(String(r.threshold)) : '0.80'}"></div>
          </div>
          <div class="form-row" id="gfRefRow"><label>SCOPE REFERENCE</label>
            <input class="input" id="gfRef" value="${esc(v.scope_ref || '')}" placeholder="Agent id, or environment name"></div>
          <div class="form-row"><label>STATUS</label><select class="filter-select w-100" id="gfStatus" style="height:34px">${opts(GR_STATUS, v.status, s => GR_STATUS_LABEL[s] || s)}</select></div>`;
      }

      function wireGuardrailForm(modal, r){
        const scope = modal.querySelector('#gfScope'), refRow = modal.querySelector('#gfRefRow');
        const paintRef = ()=>{ refRow.style.display = scope.value === 'Global' ? 'none' : ''; };
        scope.addEventListener('change', paintRef); paintRef();
        // The person choosing an unenforceable type deserves to know now, and
        // what the checker is told to look for depends on the type chosen.
        const type = modal.querySelector('#gfType'), hint = modal.querySelector('#gfTypeHint');
        const paintType = ()=>{
          hint.textContent = GR_ENFORCEABLE.includes(type.value)
            ? 'Enforced at ingest by the inline content checker.'
            : 'Recorded only: the content checker on this deployment does not implement this validation, so it cannot block or mask at ingest. Agents can still report its verdicts through the SDK.';
          modal.querySelector('#gfConfig').innerHTML = grConfigFields(type.value, r ? r.config : null);
        };
        type.addEventListener('change', paintType); paintType();
      }

      /** What the form holds, or null after saying which field needs attention. */
      function readGuardrailForm(modal, r){
        const name = modal.querySelector('#gfName').value.trim();
        if(!name){ toast('error','Name required','Give the guardrail a name.'); return null; }
        // min/max on a number input do not stop typed values, and a blank one
        // must not quietly become a default the admin never chose.
        const threshold = parseFloat(modal.querySelector('#gfThreshold').value);
        if(Number.isNaN(threshold) || threshold < 0 || threshold > 1){
          toast('error','Threshold out of range','Enter a threshold between 0 and 1.'); return null;
        }
        const scope = modal.querySelector('#gfScope').value;
        const ref = modal.querySelector('#gfRef').value.trim();
        if(scope !== 'Global' && !ref){
          toast('error','Scope reference required', `Name the ${scope === 'Agent' ? 'agent id' : 'environment'} this guardrail applies to.`);
          return null;
        }
        const type = modal.querySelector('#gfType').value;
        const config = grReadConfig(modal, type, r ? r.config : null, r ? r.guardrail_type : null);
        if(!config) return null;
        return {
          name, guardrail_type: type,
          action: modal.querySelector('#gfAction').value,
          status: modal.querySelector('#gfStatus').value,
          threshold, scope, scope_ref: scope === 'Global' ? null : ref, config,
        };
      }

      /** Say what the saved guardrail actually does to live content. */
      function announce(title, g){
        if(grNotEnforced(g)) toast('warn', `${title}, but not enforced`, grReason(g), 8000);
        else if(g.status === 'Tuning') toast('success', title, `${g.name} records detections across ${g.coverage}; nothing is enforced while it is tuning.`);
        else if(g.status === 'Disabled') toast('success', title, `${g.name} is saved but disabled.`);
        else toast('success', title, `${g.name} is enforcing across ${g.coverage}.`);
      }

      document.getElementById('grNew').addEventListener('click', () => {
        if(!allowed('admin','Creating a guardrail requires the admin role.')) return;
        openModal({
          title:'New Guardrail', icon:'shieldCheck',
          body: guardrailForm(null),
          footer:[
            { label:'Cancel' },
            { label:'Create Guardrail', cls:'primary', onClick: async (close, modal) => {
                const body = readGuardrailForm(modal, null);
                if(!body) return;
                close();
                try {
                  const created = await Store.mutate(() => API.guardrails.create(body), { event:'guardrails:changed' });
                  announce('Guardrail created', created);
                  table.refresh();
                  loadSummary();
                } catch (err) {
                  toast('error','Could not create the guardrail', errText(err));
                }
              } },
          ],
          onOpen(modal){ wireGuardrailForm(modal, null); },
        });
      });

      function openEdit(r){
        if(!allowed('admin','Editing a guardrail requires the admin role.')) return;
        openModal({
          title:'Edit Guardrail — ' + r.name, icon:'edit',
          body: guardrailForm(r),
          footer:[
            { label:'Cancel' },
            { label:'Save Changes', cls:'primary', onClick: async (close, modal) => {
                const form = readGuardrailForm(modal, r);
                if(!form) return;
                // Only what changed is sent, so the audit trail names the fields
                // that moved. `config` always travels whole: the server replaces it.
                const same = (a, b) => JSON.stringify(a == null ? null : a) === JSON.stringify(b == null ? null : b);
                const body = {};
                Object.keys(form).forEach(k => { if(!same(form[k], k === 'config' ? (r.config || {}) : r[k])) body[k] = form[k]; });
                close();
                if(!Object.keys(body).length){ toast('info','Nothing to save','No field was changed.'); return; }
                // Refused with 409 if someone else saved this guardrail since it was read.
                if(r.updated_at) body.expected_updated_at = r.updated_at;
                try {
                  const updated = await Store.mutate(() => API.guardrails.update(r.id, body), { event:'guardrails:changed' });
                  announce('Guardrail updated', updated);
                } catch (err) {
                  toast('error','Could not save the guardrail', errText(err));
                }
                // Also after a refusal: a 409 means the row on screen is stale.
                table.refresh();
                loadSummary();
              } },
          ],
          onOpen(modal){ wireGuardrailForm(modal, r); },
        });
      }

      function remove(r){
        if(!allowed('admin','Deleting a guardrail requires the admin role.')) return;
        confirmModal({
          title:'Delete Guardrail', danger:true, confirmLabel:'Delete',
          msg:`Delete ${r.name}? Its recorded detections are deleted with it; the audit trail is kept. To stop enforcing it and keep its history, disable it instead.`,
          onConfirm: async () => {
            try {
              await Store.mutate(() => API.guardrails.remove(r.id), { event:'guardrails:changed' });
              toast('success','Guardrail deleted', r.name);
              if(selectedId === r.id) closeInspector();
              table.refresh();
              loadSummary();
            } catch (err) {
              toast('error','Could not delete the guardrail', errText(err));
            }
          },
        });
      }
    },
  };

  /* ================= TESTING & REGRESSION ================= */
  const TS_KPIS = ['Total Test Suites','Tests Executed (30d)','Pass Rate (30d)','Regressions Detected','Avg. Test Duration','Flaky Tests'];
  const SUITE_TYPES = ['Regression','Evaluation','Load','Smoke','Security'];
  const SUITE_STATUS = ['Active','Draft','Disabled'];
  const ENVIRONMENTS = ['Production','Staging','UAT','Development','QA','Sandbox','DR'];
  const RUN_STATUS = ['Queued','Running','Passed','Failed','Error','Cancelled'];
  const RUN_TRIGGERS = ['Manual','Schedule','CI','Deployment'];

  function resultColor(result){
    return result === 'Passed' ? 'green' : result === 'Warning' || result === 'Running' || result === 'Queued' ? 'amber'
      : result === 'Never Run' ? 'gray' : 'red';
  }
  /** How a run ended, as opposed to the pass-rate verdict `resultColor` paints. */
  function runStatusColor(status){
    return status === 'Passed' ? 'green' : status === 'Running' || status === 'Queued' ? 'amber'
      : status === 'Cancelled' ? 'gray' : 'red';
  }

  SCREENS['testing'] = {
    title:'Testing & Regression Suite',
    render(main){
      let selected = null, activeTab = 0;
      // Suite-run polls outlive a render; the router closes them on navigation.
      const pollers = [];
      this.cleanup = () => { pollers.splice(0).forEach(p => { try { p.stop(); } catch (_) {} }); };

      main.innerHTML = `
        ${pageHead({title:'Testing & Regression Suite', sub:'Build, run, and manage evaluation, regression, and load tests to ensure AI agent reliability and quality.',
          actions:`${searchBox('tsSearch','Search test suites…')}
          <button class="btn" id="tsExport">${ICONS.download}Export</button>
          <button class="btn primary" id="tsNew"${gate('member','Creating a test suite requires the member role.')}>${ICONS.plus}Create Test Suite</button>`})}
        <div id="tsKpis">${kpiSkeleton(TS_KPIS)}</div>
        <div id="tsTabs"></div>
        <div class="with-inspector" id="tsLayout">
          <div id="tsTableWrap"><div id="tsAlt"></div></div>
          <div class="inspector" id="tsInspector"></div>
        </div>`;

      function loadSummary(){
        const host = document.getElementById('tsKpis');
        if(!host) return;
        host.innerHTML = kpiSkeleton(TS_KPIS);
        API.testing.summary()
          .then(s => {
            if(!document.getElementById('tsKpis')) return;
            const w = `vs previous ${s.window_days} days`;
            host.innerHTML = kpiRow([
              { label:'Total Test Suites', value:fmtFull(s.suites), icon:'box', color:'purple',
                delta: s.suites_delta ? Math.abs(s.suites_delta) + ' suites' : null,
                dir: dirOf(s.suites_delta), good: s.suites_delta >= 0, vs: w,
                sub: s.running ? `${s.running} running` : null },
              { label:`Tests Executed (${s.window_days}d)`, value:fmtFull(s.tests_executed), icon:'fileText', color:'blue',
                delta: s.tests_executed_delta_percent == null ? null : Math.abs(s.tests_executed_delta_percent).toFixed(1) + '%',
                dir: dirOf(s.tests_executed_delta_percent), good: s.tests_executed_delta_percent >= 0, vs: w },
              { label:`Pass Rate (${s.window_days}d)`, value: s.pass_rate == null ? '—' : pct(s.pass_rate), icon:'shieldCheck', color:'green',
                delta: s.pass_rate_delta == null ? null : Math.abs(s.pass_rate_delta).toFixed(1) + ' pp',
                dir: dirOf(s.pass_rate_delta), good: s.pass_rate_delta >= 0, vs: w },
              { label:'Regressions Detected', value:fmtFull(s.regressions_detected), icon:'bug', color:'amber',
                delta: s.regressions_delta_percent == null ? null : Math.abs(s.regressions_delta_percent).toFixed(1) + '%',
                dir: dirOf(s.regressions_delta_percent), good: s.regressions_delta_percent <= 0, vs: w },
              { label:'Avg. Test Duration', value:`<span style="font-size:20px">${esc(s.avg_duration_label || '—')}</span>`, icon:'clock', color:'cyan',
                delta: s.avg_duration_delta_percent == null ? null : Math.abs(s.avg_duration_delta_percent).toFixed(1) + '%',
                dir: dirOf(s.avg_duration_delta_percent), good: s.avg_duration_delta_percent <= 0, vs: w },
              { label:'Flaky Tests', value:fmtFull(s.flaky_tests), icon:'zap', color:'red',
                delta: s.flaky_tests_delta ? Math.abs(s.flaky_tests_delta) + ' cases' : null,
                dir: dirOf(s.flaky_tests_delta), good: s.flaky_tests_delta <= 0, vs: w,
                sub:'Cases that flipped verdict in the window' },
            ], 190);
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadSummary, 'the testing summary')); });
      }

      // ---- suites table -----------------------------------------------------
      const table = dataTable({
        columns:[
          { key:'name', label:'Suite Name', render:r => entityCell(r.name, r.dataset, 'beaker', resultColor(r.result))
              + (r.flaky_count ? ` <span class="badge bg-red" title="Cases that flipped verdict across recent runs">${ICONS.zap}${r.flaky_count} flaky</span>` : '') },
          { key:'suite_type', label:'Type', render:r => badge(r.suite_type,
              { Regression:'purple', Evaluation:'blue', Load:'cyan', Smoke:'green', Security:'red' }[r.suite_type] || 'gray') },
          { key:'environment', label:'Environment', render:r => badge(r.environment) },
          { key:'result', label:'Status', sortable:false, render:r => statusText(r.result, resultColor(r.result)) },
          { key:'pass_rate', label:'Pass Rate', sortable:false, render:r => r.pass_rate == null ? dash
              : barPct(r.pass_rate, r.pass_rate >= 90 ? 'green' : r.pass_rate >= 75 ? 'amber' : 'red') },
          { key:'last_run_at', label:'Last Run', render:r => when(r.last_run_at) },
          // The baseline is a property of a run, not a column the suite query can order by.
          { key:'baseline', label:'Baseline', sortable:false, render:r => r.baseline ? `<span class="mono">${esc(r.baseline)}</span>` : '<span class="faint">none</span>' },
          { key:'owner_name', label:'Owner', sortable:false, render:r => r.owner_name ? ownerCell(r.owner_name, r.team || '') : dash },
        ],
        rowId:'id', pageSize:10, pageSizes:[10,25,50], itemName:'test suites',
        searchPlaceholder:'Search test suites…',
        defaultSort:{ key:'name', dir:1 },
        emptyText:'No test suites are defined in this workspace yet',
        filters:[
          { key:'suite_type', label:'Type', param:'type', options:SUITE_TYPES, allLabel:'All Types' },
          { key:'status', label:'Status', param:'status', options:SUITE_STATUS, allLabel:'All Status' },
          { key:'environment', label:'Environment', param:'environment', options:ENVIRONMENTS, allLabel:'All Environments' },
          { key:'owner', label:'Owner', param:'owner', options:[], allLabel:'All Owners' },
        ],
        source: (params) => API.testing.list(params),
        exportSource: (params) => API.testing.export(params),
        // The KPI cards are workspace-wide — no page, sort, filter or search
        // changes them — and the summary reads two windows of runs, so it is
        // asked for once with the screen and again after a change.
        onLoad: fillOwners,
        autoSelectFirst: true,
        onSelect: showSuite,
        rowActions: r => [
          { label:'Run Suite', icon:'play', onClick:()=>runSuite(r) },
          { label:'View Results', icon:'chart', onClick:()=>showSuite(r) },
          { label:'Compare Baselines', icon:'git', onClick:()=>openCompare(r) },
          { label:'Edit Schedule', icon:'calendar', onClick:()=>openSchedule(r) },
          { sep:true },
          { label:'Promote Baseline', icon:'upload', onClick:()=>promote(r) },
        ],
      });

      const wrap = document.getElementById('tsTableWrap');
      wrap.insertBefore(table.filterEl, wrap.firstChild);
      wrap.insertBefore(table.el, document.getElementById('tsAlt'));
      /* The header's Search and Export belong to whichever tab is showing. They
         used to be wired to the suites table alone, so Export on the Test Runs
         tab downloaded the suites CSV (and said so in a success toast) while the
         runs export had no control anywhere. A tab with no server-side CSV
         disables the button rather than exporting something else. */
      let activeTable = null;
      function setActive(t, label, exportable){
        activeTable = t || null;
        const box = document.getElementById('tsSearch'), exp = document.getElementById('tsExport');
        if(box){
          box.disabled = !activeTable;
          box.value = activeTable ? (activeTable.state.query || '') : '';
          box.placeholder = activeTable ? `Search ${label}…` : 'Nothing to search on this tab';
        }
        if(exp){
          exp.disabled = !(activeTable && exportable);
          exp.title = exp.disabled ? 'This tab has no CSV export.' : '';
        }
      }
      setActive(table, 'test suites', true);
      document.getElementById('tsSearch').addEventListener('input', e => { if(activeTable) activeTable.search(e.target.value); });
      document.getElementById('tsExport').addEventListener('click', () => { if(activeTable) activeTable.export(); });
      loadSummary();

      function fillOwners(rows){
        const sel = table.filterEl && table.filterEl.querySelector('[data-fi="3"]');
        if(!sel) return;
        const have = new Set(Array.from(sel.options).map(o => o.value || o.textContent));
        // The `owner` query param filters on owner_user_id, so the id must be
        // the option's value; the name is only its label.
        (rows || []).forEach(r => {
          if(r.owner_user_id && r.owner_name && !have.has(r.owner_user_id)){
            have.add(r.owner_user_id);
            const o = document.createElement('option');
            o.value = r.owner_user_id;
            o.textContent = r.owner_name;
            sel.appendChild(o);
          }
        });
      }

      // ---- tabs ---------------------------------------------------------------
      const TABS = ['Test Suites','Test Runs','Datasets','Evaluations','Baselines','Schedules','Environments'];
      tabBar(document.getElementById('tsTabs'), TABS.map(l => ({ label:l })), switchTab);

      function switchTab(i){
        activeTab = i;
        const alt = document.getElementById('tsAlt');
        alt.innerHTML = '';
        const suitesOn = i === 0;
        table.el.style.display = suitesOn ? '' : 'none';
        table.filterEl.style.display = suitesOn ? '' : 'none';
        if(suitesOn){ setActive(table, 'test suites', true); return; }
        // Each tab that holds a table claims the header controls for it.
        setActive(null);
        if(i === 1) tabRuns(alt);
        else if(i === 2) tabDatasets(alt);
        else if(i === 3) tabEvaluations(alt);
        else if(i === 4) tabBaselines(alt);
        else if(i === 5) tabSchedules(alt);
        else tabEnvironments(alt);
      }

      function tabRuns(host){
        const t = dataTable({
          columns:[
            { key:'run_ref', label:'Run', render:r => `<span class="mono">${esc(r.run_ref)}</span>` },
            // The suite name lives on the joined suite, so the run query cannot order by it.
            { key:'suite_name', label:'Suite', sortable:false, render:r => `<span class="cell-main">${esc(r.suite_name || '—')}</span>` },
            { key:'trigger', label:'Trigger', render:r => badge(r.trigger, 'gray') },
            // Two different facts. `status` is how the run ended — Failed when
            // any case failed — and is what the server sorts and filters on, so
            // it is what this column shows. `result` is the pass-rate verdict
            // (90% of cases passing reads Passed): shown as `status` it made a
            // green "Passed" row vanish under Status = Passed and turn up under
            // Failed. The verdict gets its own column, and only once there is one.
            { key:'status', label:'Status', render:r => statusText(r.status, runStatusColor(r.status)) },
            { key:'result', label:'Verdict', sortable:false, render:r =>
                r.status === 'Passed' || r.status === 'Failed' ? badge(r.result, resultColor(r.result)) : dash },
            { key:'pass_rate', label:'Pass Rate', align:'right', cls:'num', render:r => r.pass_rate == null ? dash : pct(r.pass_rate) },
            { key:'total_cases', label:'Cases', align:'right', cls:'num', render:r => `${num(r.passed)} / ${num(r.total_cases)}` },
            { key:'regression_count', label:'Regressions', align:'right', cls:'num', sortable:false, render:r => num(r.regression_count) },
            { key:'duration_seconds', label:'Duration', align:'right', cls:'num', render:r => r.duration_label ? esc(r.duration_label) : dash },
            { key:'created_at', label:'Time', render:r => when(r.started_at || r.created_at) },
          ],
          rowId:'id', pageSize:10, itemName:'test runs', searchPlaceholder:'Search runs…',
          defaultSort:{ key:'created_at', dir:-1 },
          emptyText:'No test runs have been recorded yet',
          filters:[
            { key:'status', label:'Status', param:'status', options:RUN_STATUS, allLabel:'All Status' },
            { key:'trigger', label:'Trigger', param:'trigger', options:RUN_TRIGGERS, allLabel:'All Triggers' },
          ],
          source: (params) => API.testing.allRuns(params),
          exportSource: (params) => API.testing.runsExport(params),
          onSelect: (row) => openRunDetail(row),
        });
        host.appendChild(t.filterEl);
        host.appendChild(t.el);
        setActive(t, 'test runs', true);
      }

      function openRunDetail(row){
        openModal({
          title:'Test Run — ' + row.run_ref, icon:'chart', wide:true,
          body:`<div class="card-loading" style="height:200px"></div>`,
          footer:[{ label:'Close' }],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.testing.runDetail(row.id)
              .then(d => {
                body.innerHTML = `${kv([
                    ['Suite', esc(d.suite_name || '—')], ['Environment', d.environment ? badge(d.environment) : dash],
                    ['Result', statusText(d.result, resultColor(d.result))], ['Trigger', esc(d.trigger)],
                    ['Cases', `${fmtFull(d.passed)} passed · ${fmtFull(d.failed)} failed · ${fmtFull(d.skipped)} skipped · ${fmtFull(d.unscored)} unscored`],
                    ['Pass Rate', d.pass_rate == null ? dash : pct(d.pass_rate)],
                    ['Regressions', num(d.regression_count)],
                    ['Duration', d.duration_label ? esc(d.duration_label) : dash],
                  ])}
                  ${(d.cases || []).length ? `<table class="tbl mt"><thead><tr><th>Case</th><th>Status</th><th class="right">Score</th></tr></thead><tbody>
                    ${d.cases.slice(0, 60).map(c => `<tr style="cursor:default"><td class="cell-main">${esc(c.name || c.id)}</td>
                      <td>${badge(c.status, c.status === 'Passed' ? 'green' : c.status === 'Failed' ? 'red' : 'gray')}</td>
                      <td class="right num">${c.score == null ? dash : Number(c.score).toFixed(2)}</td></tr>`).join('')}
                    </tbody></table>` : emptyBlock('layers','This run recorded no per-case verdicts','')}`;
              })
              .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, null, 'this test run')); });
          },
        });
      }

      function tabDatasets(host){
        // Datasets are read straight out of the telemetry engine, which pages and
        // searches but does not order — so no column advertises a sort it cannot do.
        const t = dataTable({
          columns:[
            { key:'name', label:'Dataset', sortable:false, render:r => `<span class="cell-main mono">${esc(r.name)}</span>` },
            { key:'description', label:'Description', sortable:false, render:r => r.description ? `<span class="dim">${esc(r.description)}</span>` : dash },
            { key:'case_count', label:'Cases', align:'right', cls:'num', sortable:false, render:r => num(r.case_count) },
            { key:'experiment_count', label:'Experiments', align:'right', cls:'num', sortable:false, render:r => num(r.experiment_count) },
            { key:'tags', label:'Tags', sortable:false, render:r => (r.tags || []).length ? r.tags.map(t => `<span class="tag">${esc(t)}</span>`).join('') : dash },
            { key:'last_updated_at', label:'Last Updated', sortable:false, render:r => when(r.last_updated_at) },
            { key:'created_by', label:'Created By', sortable:false, render:r => r.created_by ? esc(r.created_by) : dash },
          ],
          rowId:'name', pageSize:10, itemName:'datasets', searchPlaceholder:'Search datasets…',
          emptyText:'This workspace has no evaluation datasets yet',
          source: (params) => API.evaluations.datasets(params),
        });
        host.appendChild(t.el);
      }

      function tabEvaluations(host){
        host.innerHTML = `<div class="card-head" style="margin-bottom:8px"><div class="card-title">Latest Evaluations</div>
          <button class="link" data-nav="evaluations">Open Evaluations ${ICONS.arrowRight}</button></div><div id="tsEvTbl"></div>`;
        const t = dataTable({
          columns:[
            { key:'agent_name', label:'Agent', render:r => entityCell(r.agent_name || r.name || '—', r.dataset, 'bot', 'purple') },
            { key:'judge_model', label:'Judge', render:r => r.judge_model ? `<span class="dim">${esc(r.judge_model)}</span>` : dash },
            { key:'cases', label:'Cases', align:'right', cls:'num', render:r => num(r.cases) },
            { key:'correctness', label:'Correctness', align:'right', sortable:false, render:r => score(r.correctness) },
            { key:'grounding', label:'Grounding', align:'right', sortable:false, render:r => score(r.grounding) },
            { key:'safety', label:'Safety', align:'right', sortable:false, render:r => score(r.safety) },
            { key:'status', label:'Status', render:r => statusText(r.status) },
            { key:'occurred_at', label:'Time', render:r => when(r.occurred_at) },
          ],
          rowId:'id', pageSize:10, itemName:'evaluations',
          defaultSort:{ key:'occurred_at', dir:-1 },
          emptyText:'No evaluations have been run yet',
          source: (params) => API.evaluations.list(params),
          onSelect: () => APP.go('evaluations'),
        });
        host.querySelector('#tsEvTbl').appendChild(t.el);
      }

      function tabBaselines(host){
        const t = dataTable({
          columns:[
            // The view is a suite query, so only the suite's own columns can order it;
            // everything derived from the two runs it compares is left unsorted.
            { key:'name', label:'Suite', render:r => `<span class="cell-main">${esc(r.suite_name)}</span>` },
            { key:'baseline', label:'Baseline', sortable:false, render:r => r.baseline ? `<span class="mono">${esc(r.baseline)}</span>` : '<span class="faint">none</span>' },
            { key:'baseline_pass_rate', label:'Baseline Pass', align:'right', cls:'num', sortable:false, render:r => r.baseline_pass_rate == null ? dash : pct(r.baseline_pass_rate) },
            { key:'candidate', label:'Candidate', sortable:false, render:r => r.candidate ? `<span class="mono">${esc(r.candidate)}</span>` : '<span class="faint">none</span>' },
            { key:'candidate_pass_rate', label:'Candidate Pass', align:'right', cls:'num', sortable:false, render:r => r.candidate_pass_rate == null ? dash : pct(r.candidate_pass_rate) },
            { key:'pass_rate_delta', label:'Δ Pass Rate', align:'right', cls:'num', sortable:false, render:r =>
                r.pass_rate_delta == null ? dash : `<span class="${r.pass_rate_delta >= 0 ? 'st-green' : 'st-red'}">${r.pass_rate_delta >= 0 ? '+' : '−'}${Math.abs(r.pass_rate_delta).toFixed(1)} pp</span>` },
            { key:'regressions', label:'Regressions', align:'right', cls:'num', sortable:false, render:r => num(r.regressions) },
          ],
          rowId:'suite_id', pageSize:10, itemName:'baselines', searchPlaceholder:'Search suites…',
          emptyText:'No suite has a run that could become a baseline yet',
          source: (params) => API.testing.baselines(params),
          rowActions: r => [
            { label: r.promotable ? 'Promote Candidate' : 'Nothing to promote', icon:'upload',
              onClick: () => r.promotable
                ? promote({ id: r.suite_id, name: r.suite_name }, r.candidate_run_id)
                : toast('info','Nothing to promote','This suite has no finished run that is not already its baseline.') },
            { label:'Compare Baselines', icon:'git', onClick:()=>{
                if(!r.baseline_run_id || !r.candidate_run_id){
                  toast('warn','Cannot compare','A baseline and a candidate run are both needed.');
                  return;
                }
                openCompareRuns(r.baseline_run_id, r.candidate_run_id, r.suite_name);
              } },
          ],
        });
        host.appendChild(t.el);
      }

      function tabSchedules(host){
        host.innerHTML = `<div class="card-head" style="margin-bottom:8px"><div class="card-title">Suite Schedules</div>
          <button class="btn sm primary" id="tsSchedNew"${gate('operator','Scheduling a suite requires the operator role.')}>${ICONS.plus}New Schedule</button></div>
          <div id="tsSchedTbl"></div>`;
        const t = dataTable({
          columns:[
            // Schedules are the suite query filtered to the scheduled ones, so the
            // orderable columns are the suite's; the cron fields are not among them.
            { key:'name', label:'Suite', render:r => `<span class="cell-main">${esc(r.suite_name)}</span>` },
            { key:'suite_type', label:'Type', render:r => badge(r.suite_type, 'purple') },
            { key:'environment', label:'Environment', render:r => badge(r.environment) },
            { key:'cron', label:'Cron', sortable:false, render:r => `<span class="mono">${esc(r.cron)}</span>` },
            { key:'cadence', label:'Cadence', sortable:false, render:r => esc(r.cadence || '—') },
            { key:'case_count', label:'Cases', align:'right', cls:'num', render:r => num(r.case_count) },
            { key:'next_run_at', label:'Next Run', sortable:false, render:r => r.next_run_at ? `<span class="dim nowrap">${fmtDateTime(ts(r.next_run_at))}</span>` : dash },
            { key:'last_run_at', label:'Last Run', render:r => when(r.last_run_at) },
            { key:'status', label:'Status', render:r => statusText(r.status, r.status === 'Active' ? 'green' : 'gray') },
          ],
          rowId:'id', pageSize:10, itemName:'schedules', searchPlaceholder:'Search schedules…',
          emptyText:'No suite is on a schedule yet',
          filters:[
            { key:'environment', label:'Environment', param:'environment', options:ENVIRONMENTS, allLabel:'All Environments' },
            { key:'suite_type', label:'Type', param:'type', options:SUITE_TYPES, allLabel:'All Types' },
          ],
          source: (params) => API.testing.schedules.list(params),
          rowActions: r => [
            { label:'Edit Cadence', icon:'edit', onClick:()=>editSchedule(r, ()=>t.refresh()) },
            { sep:true },
            { label:'Unschedule', icon:'trash', danger:true, onClick:()=>removeSchedule(r, ()=>t.refresh()) },
          ],
        });
        const holder = host.querySelector('#tsSchedTbl');
        holder.appendChild(t.filterEl);
        holder.appendChild(t.el);
        const nb = host.querySelector('#tsSchedNew');
        if(nb) nb.addEventListener('click', ()=>newSchedule(()=>t.refresh()));
      }

      function tabEnvironments(host){
        host.innerHTML = '<div class="card"><div class="card-loading" style="height:140px"></div></div>';
        API.testing.environments({ window_days: 30 })
          .then(rows => {
            const el = document.getElementById('tsAlt');
            if(!el) return;
            if(!rows.length){
              el.innerHTML = `<div class="card">${emptyBlock('globe','No environment carries a test suite yet','Create a suite and bind it to an environment.')}</div>`;
              return;
            }
            el.innerHTML = `<div class="card pad-0"><table class="tbl"><thead><tr>
                <th>Environment</th><th class="right">Suites</th><th class="right">Active</th><th class="right">Runs (30d)</th>
                <th class="right">Pass Rate</th><th class="right">Failing</th><th>Last Full Pass</th><th>Last Run</th></tr></thead><tbody>
              ${rows.map(r => `<tr style="cursor:default"><td>${badge(r.environment)}</td>
                <td class="right num">${num(r.suites)}</td><td class="right num">${num(r.active_suites)}</td>
                <td class="right num">${num(r.runs_30d)}</td>
                <td class="right num">${r.pass_rate_30d == null ? dash : pct(r.pass_rate_30d)}</td>
                <td class="right num">${num(r.failing_suites)}</td>
                <td>${when(r.last_full_pass_at)}</td><td>${when(r.last_run_at)}</td></tr>`).join('')}
              </tbody></table></div>`;
          })
          .catch(err => {
            const el = document.getElementById('tsAlt');
            if(el){ el.innerHTML = ''; el.appendChild(screenError(err, ()=>tabEnvironments(el), 'the environment bindings')); }
          });
      }

      // ---- schedule CRUD -------------------------------------------------------
      async function newSchedule(after){
        if(!allowed('operator','Scheduling a suite requires the operator role.')) return;
        let suites = { items: [] };
        // Scheduling an already-scheduled suite is a guaranteed 409, so the
        // picker only offers the suites without a cadence.
        try { suites = await API.testing.list({ page_size: 100, scheduled: false }); }
        catch (err) { toast('error','Could not load suites', errText(err)); return; }
        if(!suites.items.length){ toast('warn','No suites to schedule','Every suite already has a schedule, or none exists yet.'); return; }
        openModal({
          title:'Schedule a Suite', icon:'calendar',
          body:`<div class="form-row"><label>SUITE</label><select class="filter-select w-100" id="scSuite" style="height:34px">
              ${suites.items.map(s => `<option value="${esc(s.id)}">${esc(s.name)} — ${esc(s.environment)}</option>`).join('')}</select></div>
            <div class="form-row"><label>CRON EXPRESSION (UTC)</label><input class="input" id="scCron" value="0 2 * * *" placeholder="0 2 * * *"></div>
            <div class="quote">Minute, hour, day of month, month, day of week. <b>0 2 * * *</b> runs nightly at 02:00.</div>`,
          footer:[{ label:'Cancel' }, { label:'Create Schedule', cls:'primary', onClick: async (close, modal) => {
            const body = { suite_id: modal.querySelector('#scSuite').value, cron: modal.querySelector('#scCron').value.trim() };
            close();
            try {
              const res = await Store.mutate(() => API.testing.schedules.create(body), { event:'testing:changed' });
              toast('success','Schedule created', res.message || 'The suite will run on its cadence.');
              after();
            } catch (err) { toast('error','Could not create the schedule', errText(err)); }
          } }],
        });
      }

      function editSchedule(r, after){
        if(!allowed('operator','Changing a schedule requires the operator role.')) return;
        openModal({
          title:'Edit Cadence — ' + r.suite_name, icon:'calendar',
          body:`<div class="form-row"><label>CRON EXPRESSION (UTC)</label><input class="input" id="scCron" value="${esc(r.cron)}"></div>
            <div class="quote">Currently <b>${esc(r.cadence || r.cron)}</b>${r.next_run_at ? ` · next run ${esc(fmtDateTime(ts(r.next_run_at)))}` : ''}.</div>`,
          footer:[{ label:'Cancel' }, { label:'Save Cadence', cls:'primary', onClick: async (close, modal) => {
            const cron = modal.querySelector('#scCron').value.trim();
            close();
            try {
              const res = await Store.mutate(() => API.testing.schedules.update(r.id, { cron }), { event:'testing:changed' });
              toast('success','Cadence updated', res.message || `${r.suite_name} now runs on ${cron}.`);
              after();
            } catch (err) { toast('error','Could not update the schedule', errText(err)); }
          } }],
        });
      }

      function removeSchedule(r, after){
        if(!allowed('operator','Removing a schedule requires the operator role.')) return;
        confirmModal({
          title:'Unschedule Suite', danger:true, confirmLabel:'Unschedule',
          msg:`Remove the schedule for ${r.suite_name}? The suite itself is kept and can still be run by hand.`,
          onConfirm: async () => {
            try {
              await Store.mutate(() => API.testing.schedules.remove(r.id), { event:'testing:changed' });
              toast('success','Schedule removed', `${r.suite_name} no longer runs on a cadence.`);
              after();
            } catch (err) { toast('error','Could not unschedule', errText(err)); }
          },
        });
      }

      async function openSchedule(suite){
        if(!allowed('operator','Changing a schedule requires the operator role.')) return;
        if(!suite.schedule_cron){
          toast('warn','Not scheduled', `${suite.name} has no schedule yet — create one from the Schedules tab.`);
          return;
        }
        // A schedule shares its suite's id but has no single-row endpoint,
        // so the row the editor needs is fished out of the list.
        let page;
        try { page = await API.testing.schedules.list({ q: suite.name, page_size: 100 }); }
        catch (err) { toast('error','Could not load the schedule', errText(err)); return; }
        const row = (page.items || []).find(s => s.id === suite.id);
        if(!row){
          toast('warn','Not scheduled', `${suite.name} no longer has a schedule.`);
          return;
        }
        editSchedule(row, ()=>table.refresh());
      }

      // ---- inspector -----------------------------------------------------------
      function showSuite(row){
        const insp = document.getElementById('tsInspector');
        const layout = document.getElementById('tsLayout');
        if(!insp || !row || !layout) return;
        selected = row;
        layout.classList.remove('collapsed');
        const trend = (row.trend || []).filter(v => v != null);
        insp.innerHTML = `
          <div class="insp-head"><div class="grow"><div class="insp-title">Suite Summary</div>
            <div class="insp-sub">${esc(row.name)} ${statusText(row.result, resultColor(row.result))}</div></div>
            <button class="icon-btn insp-close" id="tsClose">${ICONS.x}</button></div>
          ${inspSection('Details','info', kv([
            ['Type', esc(row.suite_type)], ['Environment', badge(row.environment)],
            ['Lifecycle', badge(row.status)],
            ['Owner', row.owner_name ? esc(row.owner_name) + (row.team ? ` (${esc(row.team)})` : '') : dash],
            ['Dataset', `<span class="mono">${esc(row.dataset)}</span>`],
            ['Cases', num(row.case_count)],
            ['Baseline', row.baseline ? `<span class="mono">${esc(row.baseline)}</span>` : '<span class="faint">none</span>'],
            ['Last Run', row.last_run_at ? fmtDateTime(ts(row.last_run_at)) : dash],
            ['Next Run', row.next_run_at ? fmtDateTime(ts(row.next_run_at)) : '<span class="faint">not scheduled</span>'],
            ['Avg Duration', row.avg_duration_label ? esc(row.avg_duration_label) : dash],
            ['Flaky Cases', num(row.flaky_count)],
            ['Regressions', num(row.regressions)],
          ]))}
          ${inspSection('Pass Rate Trend','chart', trend.length > 1
            ? lineChart({ series:[{ color:'green', points:trend, area:true, dots:true }], h:120, yFmt:v=>Math.round(v)+'%', min:0, max:100 })
            : '<div class="faint small">This suite needs a second finished run before a trend can be drawn.</div>')}
          <div class="insp-section"><div class="insp-section-title">${ICONS.target}Last Run Results</div>
            <div id="tsRunCard">${row.last_run_id ? '<div class="card-loading" style="height:110px"></div>'
              : '<div class="faint small">This suite has never been run.</div>'}</div></div>
          ${row.regressions ? inspSection('Regressions','bug', `<div class="quote" style="border-color:rgba(239,68,68,.4)">
            <b style="color:#B91C1C">${row.regressions} regression${row.regressions > 1 ? 's' : ''} in the last finished run</b>
            <div class="small dim">Cases that passed on baseline ${esc(row.baseline || '—')} and fail now.</div></div>`) : ''}
          <div class="insp-section"><div class="insp-section-title">Quick Actions</div>
            <div class="grid g2" style="gap:8px">
              <button class="btn sm primary" id="qaRun"${gate('member','Running a suite requires the member role.')}>${ICONS.play}Run Suite</button>
              <button class="btn sm" id="qaSched">${ICONS.calendar}View Schedules</button>
              <button class="btn sm" id="qaEval">${ICONS.beaker}Create Evaluation</button>
              <button class="btn sm" id="qaComp">${ICONS.git}Compare Baselines</button>
            </div></div>`;
        insp.querySelector('#tsClose').addEventListener('click', ()=>layout.classList.add('collapsed'));
        insp.querySelector('#qaRun').addEventListener('click', ()=>runSuite(row));
        insp.querySelector('#qaSched').addEventListener('click', ()=>{ const t = document.querySelectorAll('#tsTabs .tab')[5]; if(t) t.click(); });
        insp.querySelector('#qaEval').addEventListener('click', ()=>APP.go('evaluations'));
        insp.querySelector('#qaComp').addEventListener('click', ()=>openCompare(row));
        if(row.last_run_id) loadRunCard(row);
      }

      function loadRunCard(row){
        API.testing.runDetail(row.last_run_id)
          .then(d => {
            if(!selected || selected.id !== row.id) return;
            const host = document.getElementById('tsRunCard');
            if(!host) return;
            const segs = [
              { value:d.passed, color:'green' }, { value:d.failed, color:'red' },
              { value:d.skipped, color:'amber' }, { value:d.unscored, color:'gray' },
            ].filter(s => s.value > 0);
            host.innerHTML = segs.length ? `<div class="donut-wrap">
                ${donut({ segments:segs, size:104, thickness:12, centerVal:fmtFull(d.total_cases), centerLabel:'Cases' })}
                <div class="legend grow">
                  <div class="legend-item"><span class="sw" style="background:#16A34A"></span><span class="lg-label">Passed</span><span class="lg-val">${fmtFull(d.passed)}</span></div>
                  <div class="legend-item"><span class="sw" style="background:#DC2626"></span><span class="lg-label">Failed</span><span class="lg-val">${fmtFull(d.failed)}</span></div>
                  <div class="legend-item"><span class="sw" style="background:#D97706"></span><span class="lg-label">Skipped</span><span class="lg-val">${fmtFull(d.skipped)}</span></div>
                  <div class="legend-item"><span class="sw" style="background:#94A3B8"></span><span class="lg-label">Unscored</span><span class="lg-val">${fmtFull(d.unscored)}</span></div>
                </div></div>
                <div class="small faint" style="margin-top:6px">Run ${esc(d.run_ref)} · ${esc(d.trigger)} · ${esc(d.duration_label || '—')}</div>`
              : '<div class="faint small">The last run recorded no case verdicts.</div>';
          })
          .catch(err => {
            const host = document.getElementById('tsRunCard');
            if(host){ host.innerHTML = ''; host.appendChild(screenError(err, ()=>loadRunCard(row), 'the last run')); }
          });
      }

      // ---- actions ---------------------------------------------------------------
      async function runSuite(r){
        if(!allowed('member','Running a suite requires the member role.')) return;
        let started;
        try {
          started = await Store.mutate(() => API.testing.run(r.id, { trigger:'Manual' }), { event:'testing:changed' });
        } catch (err) {
          toast('error','Could not start the run', errText(err));
          return;
        }
        toast('info','Suite queued', `${r.name} — run ${started.run_ref}.`);
        table.refresh();
        loadSummary();
        pollers.push(progressModal({
          title:'Run Progress — ' + r.name, icon:'play',
          poll: () => API.testing.progress(r.id, started.id),
          onDone: (p) => {
            table.refresh(); loadSummary();
            if(selected && selected.id === r.id) API.testing.get(r.id).then(showSuite).catch(()=>{});
            toast(p.status === 'Passed' ? 'success' : p.status === 'Running' ? 'info' : 'warn',
              'Suite finished',
              `${r.name} — ${fmtFull(p.passed)} passed, ${fmtFull(p.failed)} failed of ${fmtFull(p.total_cases)} cases.`);
          },
          onFail: () => table.refresh(),
        }));
      }

      async function promote(r, runId){
        if(!allowed('operator','Promoting a baseline requires the operator role.')) return;
        try {
          const res = await Store.mutate(() => API.testing.promoteBaseline(r.id, runId || null), { event:'testing:changed' });
          toast('success','Baseline promoted', res.message || `${r.name} has a new baseline.`);
          table.refresh();
          loadSummary();
          if(activeTab === 4) switchTab(4);
          if(selected && selected.id === r.id) API.testing.get(r.id).then(showSuite).catch(()=>{});
        } catch (err) {
          toast('error','Could not promote the baseline', errText(err));
        }
      }

      function openCompare(r){
        if(!r.baseline_run_id || !r.last_run_id){
          toast('warn','Cannot compare', 'A promoted baseline and a later finished run are both needed before a comparison exists.');
          return;
        }
        if(r.baseline_run_id === r.last_run_id){
          toast('info','Nothing to compare', 'The last run is the baseline, so there is no difference to show.');
          return;
        }
        openCompareRuns(r.baseline_run_id, r.last_run_id, r.name);
      }

      function openCompareRuns(baseline, candidate, label){
        openModal({
          title:'Baseline Comparison' + (label ? ' — ' + label : ''), icon:'git', wide:true,
          body:`<div class="card-loading" style="height:200px"></div>`,
          footer:[{ label:'Close' }],
          onOpen(modal){
            const body = modal.querySelector('.modal-body');
            API.testing.compare({ baseline, candidate })
              .then(c => {
                const vColor = c.verdict === 'Improved' ? 'green' : c.verdict === 'Regressed' ? 'red' : 'gray';
                body.innerHTML = `<div class="flex between" style="margin-bottom:10px">
                    <div><b>${esc(c.candidate.run_ref)}</b> <span class="faint">vs baseline</span> <b>${esc(c.baseline.run_ref)}</b>
                      <div class="small dim">${esc(c.candidate.suite_name || '')}</div></div>
                    ${badge(c.verdict, vColor)}</div>
                  ${kv([
                    ['Baseline Pass Rate', c.baseline.pass_rate == null ? dash : pct(c.baseline.pass_rate)],
                    ['Candidate Pass Rate', c.candidate.pass_rate == null ? dash : pct(c.candidate.pass_rate)],
                    ['Δ Pass Rate', c.pass_rate_delta == null ? dash : `<span class="${c.pass_rate_delta >= 0 ? 'st-green' : 'st-red'}">${c.pass_rate_delta >= 0 ? '+' : '−'}${Math.abs(c.pass_rate_delta).toFixed(1)} pp</span>`],
                    ['Regressions', num(c.regressions)], ['Fixes', num(c.fixes)],
                    ['Added', num(c.added)], ['Removed', num(c.removed)], ['Unchanged', num(c.unchanged)],
                  ])}
                  ${(c.cases || []).length ? `<table class="tbl mt"><thead><tr><th>Case</th><th>Baseline</th><th>Candidate</th><th>Change</th></tr></thead><tbody>
                    ${c.cases.slice(0, 80).map(x => `<tr style="cursor:default"><td class="cell-main">${esc(x.name || x.id)}</td>
                      <td>${x.baseline_status ? badge(x.baseline_status, x.baseline_status === 'Passed' ? 'green' : x.baseline_status === 'Failed' ? 'red' : 'gray') : dash}</td>
                      <td>${x.candidate_status ? badge(x.candidate_status, x.candidate_status === 'Passed' ? 'green' : x.candidate_status === 'Failed' ? 'red' : 'gray') : dash}</td>
                      <td>${badge(x.change, x.change === 'Regression' ? 'red' : x.change === 'Fix' ? 'green' : 'gray')}</td></tr>`).join('')}
                    </tbody></table>` : emptyBlock('layers','Neither run recorded per-case verdicts','')}`;
              })
              .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, null, 'the run comparison')); });
          },
        });
      }

      // ---- Create Test Suite ------------------------------------------------------
      document.getElementById('tsNew').addEventListener('click', async () => {
        if(!allowed('member','Creating a test suite requires the member role.')) return;
        let datasets = null, dsError = null, agents = { items: [] };
        try { datasets = await API.evaluations.datasets(); } catch (err) { dsError = err; }
        try { agents = await API.agents.list({ page_size: 100 }); } catch (_) { /* optional */ }

        const dsField = dsError
          ? `<div class="quote" style="border-color:rgba(239,68,68,.4)"><b style="color:#B91C1C">Datasets unavailable</b>
               <div class="small dim">${esc(dsError.message)}</div></div>`
          : (datasets.items || []).length
            ? `<select class="filter-select w-100" id="ntsDs" style="height:34px">${datasets.items.map(d =>
                `<option value="${esc(d.name)}">${esc(d.name)} — ${fmtFull(d.case_count)} cases</option>`).join('')}</select>`
            : `<div class="quote"><b>No datasets yet</b><div class="small dim">A suite runs over a dataset, and this workspace has none.</div></div>`;

        openModal({
          title:'Create Test Suite', icon:'beaker',
          body:`<div class="form-row"><label>SUITE NAME</label><input class="input" id="ntsName" placeholder="e.g. Vendor Onboarding Regression"></div>
            <div class="grid g2">
              <div class="form-row"><label>TYPE</label><select class="filter-select w-100" id="ntsType" style="height:34px">${SUITE_TYPES.map(t=>`<option>${t}</option>`).join('')}</select></div>
              <div class="form-row"><label>ENVIRONMENT</label><select class="filter-select w-100" id="ntsEnv" style="height:34px">${ENVIRONMENTS.map(e=>`<option ${e === 'Staging' ? 'selected' : ''}>${e}</option>`).join('')}</select></div>
            </div>
            <div class="form-row"><label>DATASET</label>${dsField}</div>
            <div class="form-row"><label>AGENT (OPTIONAL)</label><select class="filter-select w-100" id="ntsAgent" style="height:34px">
              <option value="">Not tied to one agent</option>
              ${(agents.items || []).map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('')}</select></div>
            <div class="form-row"><label>SCHEDULE (CRON, OPTIONAL)</label><input class="input" id="ntsCron" placeholder="e.g. 0 2 * * *"></div>`,
          footer:[
            { label:'Cancel' },
            { label:'Create Suite', cls:'primary', onClick: async (close, modal) => {
                const dsEl = modal.querySelector('#ntsDs');
                if(!dsEl){ toast('error','Cannot create','There is no dataset for the suite to run over.'); return; }
                const name = modal.querySelector('#ntsName').value.trim();
                if(!name){ toast('error','Name required','Give the suite a name before creating it.'); return; }
                const body = {
                  name, suite_type: modal.querySelector('#ntsType').value,
                  environment: modal.querySelector('#ntsEnv').value,
                  dataset: dsEl.value, status:'Active',
                };
                const agentId = modal.querySelector('#ntsAgent').value;
                if(agentId) body.agent_id = agentId;
                const cron = modal.querySelector('#ntsCron').value.trim();
                if(cron) body.schedule_cron = cron;
                close();
                try {
                  const created = await Store.mutate(() => API.testing.create(body), { event:'testing:changed' });
                  toast('success','Test suite created', `${created.name} covers ${fmtFull(created.case_count)} cases in ${created.environment}.`);
                  table.refresh();
                  loadSummary();
                } catch (err) {
                  toast('error','Could not create the suite', errText(err));
                }
              } },
          ],
          onOpen(modal){
            if(dsError || !(datasets.items || []).length){
              const btn = modal.querySelector('[data-mbtn="1"]');
              if(btn){ btn.disabled = true; btn.title = 'A dataset is required before a suite can be created.'; }
            }
          },
        });
      });
    },
  };

  /* ================= FEEDBACK & QUALITY LOOP ================= */
  const FB_PRIMARY = ['Total Feedback (30d)','Avg. Rating','Positive Feedback','Issues Identified','Improvements Deployed','SLA Met'];
  const FB_SECONDARY = ['Negative','Neutral','Rated Feedback','Clustered','Open Issues','Overdue Issues','Backlog Open','Backlog In Progress','Resolved Issues'];
  const SENTIMENTS = ['Positive','Neutral','Negative'];
  const FB_SOURCES = ['End User (In-App)','Agent Response Rating','Support Ticket','Manual Review'];
  const ISSUE_SEVERITY = ['Critical','High','Medium','Low'];
  const ISSUE_STATUS = ['Open','In Progress','Resolved','Wont Fix'];
  const BACKLOG_PRIORITY = ['P0','P1','P2','P3'];
  const BACKLOG_STATUS = ['Backlog','Planned','In Progress','Done'];

  SCREENS['feedback'] = {
    title:'Feedback & Quality Loop',
    render(main){
      let mainTable = null, activeTab = 0, summaryCache = null, selectedFbId = null;

      main.innerHTML = `
        ${pageHead({title:'Feedback & Quality Loop', sub:'Capture feedback, analyze quality signals, prioritize improvements, and drive continuous AI agent excellence.',
          actions:`${searchBox('fbSearch','Search feedback…')}
          <button class="btn" id="fbExport">${ICONS.download}Export</button>
          <button class="btn primary" id="fbSubmit"${gate('member','Submitting feedback requires the member role.')}>${ICONS.plus}Submit Feedback</button>`})}
        <div id="fbKpis">${kpiSkeleton(FB_PRIMARY)}</div>
        <div id="fbKpis2" class="mt">${kpiSkeleton(FB_SECONDARY, 150)}</div>
        <div id="fbTabs"></div>
        <div id="fbBody"></div>`;

      const body = document.getElementById('fbBody');

      function loadSummary(then){
        const host = document.getElementById('fbKpis'), host2 = document.getElementById('fbKpis2');
        if(!host) return;
        host.innerHTML = kpiSkeleton(FB_PRIMARY);
        if(host2) host2.innerHTML = kpiSkeleton(FB_SECONDARY, 150);
        API.feedback.summary({ window_days: 30 })
          .then(s => {
            summaryCache = s;
            if(!document.getElementById('fbKpis')) return;
            const w = `vs previous ${s.window_days} days`;
            host.innerHTML = kpiRow([
              { label:`Total Feedback (${s.window_days}d)`, value:fmtFull(s.total_feedback), icon:'chat', color:'purple',
                delta: s.total_feedback_delta_percent == null ? null : Math.abs(s.total_feedback_delta_percent).toFixed(1) + '%',
                dir: dirOf(s.total_feedback_delta_percent), good: s.total_feedback_delta_percent >= 0, vs: w },
              { label:'Avg. Rating', value: s.avg_rating == null ? '—' : `${s.avg_rating.toFixed(2)} <span class="unit">/ 5</span>`, icon:'star', color:'green',
                delta: s.avg_rating_delta == null ? null : Math.abs(s.avg_rating_delta).toFixed(2),
                dir: dirOf(s.avg_rating_delta), good: s.avg_rating_delta >= 0, vs: w },
              { label:'Positive Feedback', value: pct(s.positive_percent), icon:'thumbsUp', color:'blue',
                delta: s.positive_percent_delta_pp == null ? null : Math.abs(s.positive_percent_delta_pp).toFixed(1) + ' pp',
                dir: dirOf(s.positive_percent_delta_pp), good: s.positive_percent_delta_pp >= 0, vs: w },
              { label:'Issues Identified', value:fmtFull(s.issues_identified), icon:'alert', color:'amber',
                delta: s.issues_identified_delta_percent == null ? null : Math.abs(s.issues_identified_delta_percent).toFixed(1) + '%',
                dir: dirOf(s.issues_identified_delta_percent), good: s.issues_identified_delta_percent <= 0, vs: w },
              { label:'Improvements Deployed', value:fmtFull(s.improvements_deployed), icon:'rocket', color:'cyan',
                delta: s.improvements_deployed_delta_percent == null ? null : Math.abs(s.improvements_deployed_delta_percent).toFixed(1) + '%',
                dir: dirOf(s.improvements_deployed_delta_percent), good: s.improvements_deployed_delta_percent >= 0, vs: w },
              { label:'SLA Met', value: s.sla_met_percent == null ? '—' : pct(s.sla_met_percent), icon:'shield', color:'red',
                delta: s.sla_met_delta_pp == null ? null : Math.abs(s.sla_met_delta_pp).toFixed(1) + ' pp',
                dir: dirOf(s.sla_met_delta_pp), good: s.sla_met_delta_pp >= 0, vs: w,
                sub: s.sla_met_percent == null ? 'No issue has reached its SLA yet' : null },
            ]);
            if(host2) host2.innerHTML = kpiRow([
              { label:'Negative', value: pct(s.negative_percent), icon:'thumbsDown', color:'red', sub:'of the window' },
              { label:'Neutral', value: pct(s.neutral_percent), icon:'chat', color:'gray', sub:'of the window' },
              { label:'Rated Feedback', value:fmtFull(s.rated_feedback), icon:'star', color:'amber', sub:'carry a star rating' },
              { label:'Clustered', value: pct(s.clustered_percent), icon:'layers', color:'purple', sub:'assigned to a theme' },
              { label:'Open Issues', value:fmtFull(s.open_issues), icon:'bug', color:'red', sub:'not yet resolved' },
              { label:'Overdue Issues', value:fmtFull(s.overdue_issues), icon:'clock', color:'red', sub:'past their SLA' },
              { label:'Backlog Open', value:fmtFull(s.backlog_open), icon:'box', color:'blue', sub:'awaiting planning' },
              { label:'Backlog In Progress', value:fmtFull(s.backlog_in_progress), icon:'zap', color:'amber', sub:'being worked' },
              { label:'Resolved Issues', value:fmtFull(s.resolved_issues), icon:'checkCircle', color:'green', sub:'closed out' },
            ], 150);
            if(then) then(s);
          })
          .catch(err => {
            host.innerHTML = ''; host.appendChild(screenError(err, ()=>loadSummary(then), 'the feedback summary'));
            if(host2) host2.innerHTML = '';
          });
      }

      // ---- the feedback table -------------------------------------------------
      function feedbackTable(pageSize){
        return dataTable({
          columns:[
            { key:'feedback_ref', label:'Feedback ID', render:r => `<span class="mono">${esc(r.feedback_ref)}</span>` },
            // The server orders this column as `agent`; the value it returns is agent_name.
            { key:'agent', label:'Agent', render:r => r.agent_name ? `<span class="cell-main" style="font-size:12px">${esc(r.agent_name)}</span>` : dash },
            { key:'trace_id', label:'Run ID', sortable:false, render:r => r.trace_id ? `<span class="mono dim">${esc(String(r.trace_id).slice(0,12))}…</span>` : dash },
            { key:'rating', label:'Rating', render:r => r.rating == null ? dash : starRating(r.rating) },
            { key:'sentiment', label:'Sentiment', render:r => badge(r.sentiment, r.sentiment === 'Positive' ? 'green' : r.sentiment === 'Neutral' ? 'amber' : 'red') },
            { key:'body', label:'Feedback', sortable:false, render:r => r.body
                ? `<span class="dim" style="max-width:250px;display:inline-block;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom">${esc(r.body)}</span>`
                : '<span class="faint">no comment</span>' },
            { key:'theme', label:'Theme', sortable:false, render:r => r.theme ? `<span class="tag">${esc(r.theme)}</span>` : '<span class="faint">unclustered</span>' },
            { key:'source', label:'Source', render:r => `<span class="dim small">${esc(r.source)}</span>` },
            { key:'submitted_at', label:'Time', render:r => when(r.submitted_at) },
          ],
          rowId:'id', pageSize: pageSize || 10, pageSizes:[10,25,50], itemName:'feedback',
          searchPlaceholder:'Search comments, themes, submitters…',
          defaultSort:{ key:'submitted_at', dir:-1 },
          emptyText:'No feedback has been captured in this workspace yet',
          filters:[
            { key:'sentiment', label:'Sentiment', param:'sentiment', options:SENTIMENTS, allLabel:'All Sentiment' },
            { key:'source', label:'Source', param:'source', options:FB_SOURCES, allLabel:'All Sources' },
            { key:'rating', label:'Rating', param:'rating', options:['1','2','3','4','5'], allLabel:'All Ratings' },
          ],
          source: (params) => API.feedback.list(params),
          exportSource: (params) => API.feedback.export(params),
          autoSelectFirst: true,
          onSelect: showFeedback,
          rowActions: r => [
            { label:'Create Issue', icon:'bug', onClick:()=>openCreateIssue({ feedback_ids:[r.id], title:(r.body || '').slice(0,80), agent_id:r.agent_id, theme:r.theme }) },
            { label:'Add to Backlog', icon:'plus', onClick:()=>openAddBacklog(r) },
            { label:'Assign to Team', icon:'users', onClick:()=>openAssignTeam(r) },
            { sep:true },
            // Feedback with a trace opens that exact run in Replay Studio;
            // without one there is no run to show, so the item is not offered.
            ...(r.trace_id ? [{ label:'View Run', icon:'replay',
              onClick:()=>{ APP.replayRun = r.trace_id; APP.go('replay'); } }] : []),
          ],
        });
      }

      function showFeedback(r){
        const insp = document.getElementById('fbInspector');
        if(!insp || !r) return;
        selectedFbId = r.id;
        const layout = document.getElementById('fbLayout');
        if(layout) layout.classList.remove('collapsed');
        insp.innerHTML = `
          <div class="insp-head"><div class="grow"><div class="insp-title">Feedback Details</div>
            <div style="margin-top:5px">${badge(r.sentiment, r.sentiment === 'Positive' ? 'green' : r.sentiment === 'Neutral' ? 'amber' : 'red')}
              ${r.theme ? `<span class="tag">${esc(r.theme)}</span>` : ''}</div></div>
            <button class="icon-btn insp-close" id="fbClose">${ICONS.x}</button></div>
          ${inspSection('Rating','star', r.rating == null
            ? '<div class="faint small">This item carries no star rating.</div>'
            : `<div class="flex between">${starRating(r.rating)}<b>${r.rating.toFixed(1)} / 5</b></div>`)}
          ${inspSection('Details','info', kv([
            ['Feedback ID', `<span class="mono small">${esc(r.feedback_ref)}</span>`],
            ['Agent', r.agent_name ? esc(r.agent_name) : dash],
            ['Run ID', r.trace_id ? `<span class="mono small">${esc(r.trace_id)}</span>` : dash],
            ['Submitted By', r.submitted_by ? esc(r.submitted_by) : dash],
            ['Source', esc(r.source)],
            ['Environment', r.environment ? badge(r.environment) : dash],
            ['Time', fmtDateTime(ts(r.submitted_at))],
            ['Theme', r.theme ? esc(r.theme) : '<span class="faint">not clustered yet</span>'],
            ['Linked Issue', r.issue_ref ? `<span class="mono">${esc(r.issue_ref)}</span> ${esc(r.issue_title || '')}` : '<span class="faint">none</span>'],
            ['Scored on Trace', r.scored_in_telemetry ? '<span class="st-green">Yes</span>' : '<span class="faint">No</span>'],
          ]))}
          ${inspSection('Feedback','chat', `<div class="quote">${esc(r.body || 'No comment was left.')}</div>
            ${(r.tags || []).length ? `<div style="margin-top:6px">${r.tags.map(t => `<span class="tag">${esc(t)}</span>`).join('')}</div>` : ''}`)}
          <div class="insp-section"><div class="insp-section-title">Quick Actions</div>
            <div class="grid g2" style="gap:8px">
              <button class="btn sm" id="fbIssue">${ICONS.bug}Create Issue</button>
              <button class="btn sm" id="fbBacklog">${ICONS.plus}Add to Backlog</button>
              <button class="btn sm" id="fbAssign">${ICONS.users}Assign to Team</button>
              <button class="btn sm primary" id="fbAnalyze">${ICONS.beaker}Analyze Feedback</button>
            </div></div>`;
        insp.querySelector('#fbClose').addEventListener('click', ()=>{ const l = document.getElementById('fbLayout'); if(l) l.classList.add('collapsed'); });
        insp.querySelector('#fbIssue').addEventListener('click', ()=>openCreateIssue({ feedback_ids:[r.id], title:(r.body || '').slice(0,80), agent_id:r.agent_id, theme:r.theme }));
        insp.querySelector('#fbBacklog').addEventListener('click', ()=>openAddBacklog(r));
        insp.querySelector('#fbAssign').addEventListener('click', ()=>openAssignTeam(r));
        insp.querySelector('#fbAnalyze').addEventListener('click', ()=>openAnalyze({ agent_id: r.agent_id }));
      }

      // ---- tabs -------------------------------------------------------------------
      const FB_TABS = ['Overview','Feedback','Quality Insights','Issues','Improvement Backlog','Actions','Reports','Settings'];
      tabBar(document.getElementById('fbTabs'), FB_TABS.map(l => ({ label:l })), renderTab);

      function switchTab(i){
        document.querySelectorAll('#fbTabs .tab').forEach((t, ti) => t.classList.toggle('active', ti === i));
        renderTab(i);
      }

      function renderTab(i){
        activeTab = i;
        mainTable = null;
        if(i === 0) tabOverview();
        else if(i === 1) tabFeedback();
        else if(i === 2) tabInsights();
        else if(i === 3) tabIssues();
        else if(i === 4) tabBacklog();
        else if(i === 5) tabActions();
        else if(i === 6) tabReports();
        else tabSettings();
      }

      function tabOverview(){
        body.innerHTML = `<div class="grid g3" id="fbCharts">
            <div class="card"><div class="card-loading" style="height:200px"></div></div>
            <div class="card"><div class="card-loading" style="height:200px"></div></div>
            <div class="card"><div class="card-loading" style="height:200px"></div></div>
          </div>
          <div id="fbFunnel" class="mt"></div>
          <div class="with-inspector mt" id="fbLayout">
            <div id="fbTableWrap">
              <div class="card-head" style="margin-bottom:8px"><div class="card-title">Recent Feedback</div>
                <button class="link" id="fbViewAll">View All ${ICONS.arrowRight}</button></div>
              <div id="fbTbl"></div>
            </div>
            <div style="display:flex;flex-direction:column;gap:14px">
              <div class="inspector" id="fbInspector" style="position:static;max-height:none"></div>
              <div class="card" id="fbTopIssues"><div class="card-loading" style="height:120px"></div></div>
              <div class="card" id="fbRecentImp"><div class="card-loading" style="height:120px"></div></div>
            </div>
          </div>`;
        mainTable = feedbackTable(10);
        document.getElementById('fbTbl').appendChild(mainTable.filterEl);
        document.getElementById('fbTbl').appendChild(mainTable.el);
        document.getElementById('fbViewAll').addEventListener('click', ()=>switchTab(1));
        loadOverviewCharts();
        loadFunnel();
        loadTopIssues();
        loadRecentImprovements();
      }

      function loadOverviewCharts(){
        const host = document.getElementById('fbCharts');
        if(!host) return;
        Promise.all([API.feedback.insights({ window_days: 30 }), summaryCache ? Promise.resolve(summaryCache) : API.feedback.summary({ window_days: 30 })])
          .then(([ins, s]) => {
            if(!document.getElementById('fbCharts')) return;
            summaryCache = s;
            const daily = ins.daily || [];
            const trendCard = daily.length > 1
              ? `${lineChart({ series:[{ color:'purple', points: daily.map(d=>d.count), area:true }], h:170,
                  xLabels: daily.map(d=>d.date.slice(5)), maxXLabels:7 })}
                 <div class="legend inline" style="margin-top:5px"><span class="legend-item"><span class="sw" style="background:#6D4AEF"></span><span class="lg-label">Feedback per day</span></span></div>`
              : emptyBlock('chart','Not enough days to plot a trend', daily.length ? 'One day of feedback so far.' : 'No feedback in this window.');
            const sent = (s.sentiment_breakdown || []).filter(x => x.count > 0);
            const sentCard = sent.length
              ? `<div class="donut-wrap">${donut({
                  segments: sent.map(x => ({ value:x.count, color: x.label === 'Positive' ? 'green' : x.label === 'Neutral' ? 'blue' : 'red' })),
                  size:132, thickness:15, centerVal:fmtFull(s.total_feedback), centerLabel:'Total' })}
                <div class="legend grow">${(s.sentiment_breakdown || []).map(x =>
                  `<div class="legend-item"><span class="sw" style="background:${x.label === 'Positive' ? '#16A34A' : x.label === 'Neutral' ? '#2563EB' : '#DC2626'}"></span>
                    <span class="lg-label">${esc(x.label)}</span><span class="lg-val">${fmtFull(x.count)}<br><span class="faint small">(${x.percent.toFixed(1)}%)</span></span></div>`).join('')}
                </div></div>`
              : emptyBlock('chat','No feedback in this window','');
            const src = (s.source_breakdown || []).filter(x => x.count > 0);
            const srcCard = src.length
              ? hbars(src.map(x => ({ label:x.label, value:x.count, color:'purple', display:fmtFull(x.count), pct:x.percent.toFixed(1) + '%' })), { labelW:150 })
              : emptyBlock('chat','No feedback has been attributed to a source yet','');
            host.innerHTML = `
              <div class="card"><div class="card-head"><div class="card-title">Feedback Trend</div><div class="faint small">Last ${ins.window_days} days</div></div>${trendCard}</div>
              <div class="card"><div class="card-head"><div class="card-title">Feedback by Sentiment</div></div>${sentCard}</div>
              <div class="card"><div class="card-head"><div class="card-title">Feedback by Source</div><button class="link" id="fbSrcAll">View All</button></div>${srcCard}</div>`;
            const sa = document.getElementById('fbSrcAll');
            if(sa) sa.addEventListener('click', ()=>switchTab(1));
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadOverviewCharts, 'the overview charts')); });
      }

      function loadFunnel(){
        const host = document.getElementById('fbFunnel');
        if(!host) return;
        host.innerHTML = `<div class="card"><div class="card-loading" style="height:140px"></div></div>`;
        API.feedback.funnel({ window_days: 30 })
          .then(f => {
            if(!document.getElementById('fbFunnel')) return;
            const stages = f.stages || [];
            host.innerHTML = `<div class="card">
              <div class="card-head"><div class="card-title">Feedback → Outcome Funnel</div>
                <div class="faint small">Last ${f.window_days} days</div></div>
              ${stages.some(s => s.count > 0)
                ? hbars(stages.map((s, i) => ({
                    label: s.stage, value: s.count,
                    color: i < 2 ? 'purple' : i < 4 ? 'amber' : 'green',
                    display: fmtFull(s.count),
                    pct: s.conversion_from_previous == null ? '—' : s.conversion_from_previous.toFixed(0) + '%',
                  })), { labelW:190 })
                : emptyBlock('chat','No feedback has entered the loop in this window','')}
              <div class="small faint" style="margin-top:6px">The right-hand figure is the share of the stage before it that reached this one.</div>
            </div>`;
          })
          .catch(err => { host.innerHTML = ''; host.appendChild(screenError(err, loadFunnel, 'the outcome funnel')); });
      }

      function loadTopIssues(){
        const host = document.getElementById('fbTopIssues');
        if(!host) return;
        // No "not resolved" filter exists server-side, so the panel ranks every
        // issue by reports and shows each one's status rather than hiding the
        // In Progress ones the "Open Issues" card is counting.
        API.feedback.issues.list({ page_size: 6, sort: '-reports' })
          .then(page => {
            const el = document.getElementById('fbTopIssues');
            if(!el) return;
            el.innerHTML = `<div class="card-head"><div class="card-title">Top Issues (30 Days)</div>
                <button class="link" id="fbIssuesAll">View All</button></div>
              ${page.items.length
                ? page.items.map(x => `<div class="hbar-row"><div class="hb-label" style="width:150px" title="${esc(x.title)} — ${esc(x.status)}">${esc(x.title)}</div>
                    <div class="hb-bar"><div class="bar-bg"><div class="bar-fill" style="width:${Math.min(100, (x.reports_30d / Math.max(1, page.items[0].reports_30d)) * 100)}%;background:${x.severity === 'Critical' || x.severity === 'High' ? 'var(--red)' : 'var(--amber)'}"></div></div></div>
                    <div class="hb-val">${x.reports_30d}</div>
                    <div style="margin-left:6px">${badge(x.status, x.status === 'Open' ? 'red' : x.status === 'In Progress' ? 'amber' : x.status === 'Resolved' ? 'green' : 'gray')}</div></div>`).join('')
                : '<div class="faint small">No issue has been opened from feedback yet.</div>'}`;
            const ia = document.getElementById('fbIssuesAll');
            if(ia) ia.addEventListener('click', ()=>switchTab(3));
          })
          .catch(err => {
            const el = document.getElementById('fbTopIssues');
            if(el){ el.innerHTML = ''; el.appendChild(screenError(err, loadTopIssues, 'the top issues')); }
          });
      }

      function loadRecentImprovements(){
        const host = document.getElementById('fbRecentImp');
        if(!host) return;
        API.feedback.improvements.list({ page_size: 6, sort: '-created_at' })
          .then(page => {
            const el = document.getElementById('fbRecentImp');
            if(!el) return;
            el.innerHTML = `<div class="card-head"><div class="card-title">Recent Improvements</div>
                <button class="link" id="fbImpAll">View All</button></div>
              ${page.items.length
                ? page.items.map(x => `<div class="kv"><span class="k" style="color:var(--text)">${esc(x.title)}</span>
                    <span class="v">${badge(x.status, x.status === 'Deployed' ? 'green' : 'amber')}${x.verified ? ' ' + badge('Verified','green') : ''}</span></div>`).join('')
                : '<div class="faint small">No fix has been recorded yet.</div>'}`;
            const im = document.getElementById('fbImpAll');
            if(im) im.addEventListener('click', ()=>switchTab(4));
          })
          .catch(err => {
            const el = document.getElementById('fbRecentImp');
            if(el){ el.innerHTML = ''; el.appendChild(screenError(err, loadRecentImprovements, 'recent improvements')); }
          });
      }

      function tabFeedback(){
        body.innerHTML = `<div class="with-inspector" id="fbLayout"><div id="fbTbl2"></div><div class="inspector" id="fbInspector"></div></div>`;
        mainTable = feedbackTable(25);
        document.getElementById('fbTbl2').appendChild(mainTable.filterEl);
        document.getElementById('fbTbl2').appendChild(mainTable.el);
      }

      function tabInsights(){
        body.innerHTML = `<div class="grid g2" id="fbIns">
          <div class="card"><div class="card-loading" style="height:200px"></div></div>
          <div class="card"><div class="card-loading" style="height:200px"></div></div>
          <div class="card"><div class="card-loading" style="height:200px"></div></div>
          <div class="card"><div class="card-loading" style="height:200px"></div></div></div>`;
        API.feedback.insights({ window_days: 30 })
          .then(ins => {
            const host = document.getElementById('fbIns');
            if(!host) return;
            const daily = ins.daily || [];
            const byAgent = ins.rating_by_agent || [];
            const themes = ins.top_themes || [];
            host.innerHTML = `
              <div class="card"><div class="card-head"><div class="card-title">Rating by Agent</div></div>
                ${byAgent.length
                  ? hbars(byAgent.filter(a => a.avg_rating != null).map(a => ({
                      label:a.agent_name, value:a.avg_rating, color:'green',
                      display:a.avg_rating.toFixed(2) + ' ★', pct:fmtFull(a.feedback_count) })), { labelW:190 })
                  : emptyBlock('star','No feedback has been attributed to an agent yet','')}</div>
              <div class="card"><div class="card-head"><div class="card-title">Sentiment Over Time</div></div>
                ${daily.length > 1
                  ? `${lineChart({ series:[
                        { color:'green', points: daily.map(d=>d.positive_percent), area:true },
                        { color:'red', points: daily.map(d=>d.negative_percent) }],
                      h:190, min:0, max:100, yFmt:v=>Math.round(v)+'%', xLabels: daily.map(d=>d.date.slice(5)), maxXLabels:7 })}
                    <div class="legend inline" style="margin-top:5px">
                      <span class="legend-item"><span class="sw" style="background:#16A34A"></span><span class="lg-label">Positive %</span></span>
                      <span class="legend-item"><span class="sw" style="background:#DC2626"></span><span class="lg-label">Negative %</span></span></div>`
                  : emptyBlock('chart','Not enough days to plot sentiment','')}</div>
              <div class="card"><div class="card-head"><div class="card-title">Top Themes</div>
                  <button class="btn sm" id="fbInsAnalyze"${gate('member','Analyzing feedback requires the member role.')}>${ICONS.beaker}Analyze</button></div>
                ${themes.length
                  ? `<table class="tbl"><thead><tr><th>Theme</th><th class="right">Items</th><th class="right">Negative</th><th class="right">Avg Rating</th><th>Issue</th></tr></thead><tbody>
                      ${themes.map(t => `<tr style="cursor:default"><td class="cell-main">${esc(t.theme)}</td>
                        <td class="right num">${fmtFull(t.size)}</td>
                        <td class="right num">${pct(t.negative_share * 100, 0)}</td>
                        <td class="right num">${t.avg_rating == null ? dash : t.avg_rating.toFixed(2)}</td>
                        <td>${t.open_issue_id ? badge('Open issue','amber') : '<span class="faint">none</span>'}</td></tr>`).join('')}
                    </tbody></table>`
                  : emptyBlock('layers','No themes yet','Run Analyze Feedback to cluster the comments that have come in.')}</div>
              <div class="card"><div class="card-head"><div class="card-title">Feedback → Outcome Funnel</div></div>
                <div id="fbIns2"><div class="card-loading" style="height:140px"></div></div></div>`;
            const ab = document.getElementById('fbInsAnalyze');
            if(ab) ab.addEventListener('click', ()=>openAnalyze({}));
            API.feedback.funnel({ window_days: 30 })
              .then(f => {
                const el = document.getElementById('fbIns2');
                if(!el) return;
                el.innerHTML = (f.stages || []).some(s => s.count > 0)
                  ? hbars(f.stages.map((s, i) => ({ label:s.stage, value:s.count, color: i < 2 ? 'purple' : i < 4 ? 'amber' : 'green',
                      display:fmtFull(s.count), pct: s.percent_of_received.toFixed(0) + '%' })), { labelW:190 })
                  : emptyBlock('chat','Nothing has entered the loop in this window','');
              })
              .catch(err => { const el = document.getElementById('fbIns2'); if(el){ el.innerHTML = ''; el.appendChild(screenError(err, null, 'the funnel')); } });
          })
          .catch(err => {
            const host = document.getElementById('fbIns');
            if(host){ host.innerHTML = ''; host.appendChild(screenError(err, tabInsights, 'the quality insights')); }
          });
      }

      function tabIssues(){
        body.innerHTML = '<div id="fbIssuesTbl"></div>';
        const t = dataTable({
          columns:[
            { key:'issue_ref', label:'Ref', render:r => `<span class="mono">${esc(r.issue_ref)}</span>` },
            { key:'title', label:'Issue', render:r => `<div><div class="cell-main">${esc(r.title)}</div>${r.description ? `<div class="cell-sub">${esc(r.description.slice(0,70))}</div>` : ''}</div>` },
            { key:'theme', label:'Category', sortable:false, render:r => r.theme ? `<span class="tag">${esc(r.theme)}</span>` : dash },
            // Ordered server-side as `reports`; the count it returns is reports_30d.
            { key:'reports', label:'Reports (30d)', align:'right', cls:'num', render:r => num(r.reports_30d) },
            { key:'severity', label:'Severity', render:r => riskBadge(r.severity) },
            { key:'agent_name', label:'Linked Agent', sortable:false, render:r => r.agent_name ? esc(r.agent_name) : dash },
            { key:'status', label:'Status', render:r => badge(r.status, r.status === 'Open' ? 'red' : r.status === 'In Progress' ? 'amber' : r.status === 'Resolved' ? 'green' : 'gray') },
            { key:'assigned_team', label:'Team', sortable:false, render:r => r.assigned_team ? esc(r.assigned_team) : '<span class="faint">unassigned</span>' },
            { key:'sla_due_at', label:'SLA', render:r => r.sla_due_at
                ? `<span class="${r.overdue ? 'st-red' : 'dim'} nowrap">${esc(until(r.sla_due_at))}</span>` : dash },
          ],
          rowId:'id', pageSize:10, itemName:'issues', searchPlaceholder:'Search issues…',
          defaultSort:{ key:'reports', dir:-1 },
          emptyText:'No issues have been opened from feedback yet',
          filters:[
            { key:'status', label:'Status', param:'status', options:ISSUE_STATUS, allLabel:'All Status' },
            { key:'severity', label:'Severity', param:'severity', options:ISSUE_SEVERITY, allLabel:'All Severities' },
            { key:'overdue', label:'SLA', param:'overdue', options:['true','false'], allLabel:'All' },
          ],
          source: (params) => API.feedback.issues.list(params),
          rowActions: r => [
            { label:'Create Fix Task', icon:'tool', onClick:()=>openFixTaskFromIssue(r, ()=>t.refresh()) },
            { label:'Add to Backlog', icon:'plus', onClick:()=>promoteIssue(r, ()=>t.refresh()) },
            { label:'Assign to Team', icon:'users', onClick:()=>openAssignIssue(r, ()=>t.refresh()) },
            { sep:true },
            // Wont Fix cannot be resolved, so only the live statuses offer it.
            ...(r.status === 'Open' || r.status === 'In Progress' ? [{ label:'Resolve Issue', icon:'checkCircle', onClick:()=>resolveIssue(r, ()=>t.refresh()) }] : []),
          ],
        });
        const holder = document.getElementById('fbIssuesTbl');
        holder.appendChild(t.filterEl);
        holder.appendChild(t.el);
      }

      function tabBacklog(){
        body.innerHTML = '<div id="fbBacklogTbl"></div>';
        const t = dataTable({
          columns:[
            { key:'title', label:'Improvement', render:r => `<div><div class="cell-main">${esc(r.title)}</div>${r.issue_ref ? `<div class="cell-sub">from ${esc(r.issue_ref)}</div>` : ''}</div>` },
            { key:'priority', label:'Priority', render:r => badge(r.priority, r.priority === 'P0' ? 'red' : r.priority === 'P1' ? 'amber' : 'gray') },
            // Votes are the reports counted behind the item, not a stored column.
            { key:'votes', label:'Votes', align:'right', cls:'num', sortable:false, render:r => num(r.votes) },
            { key:'status', label:'Status', render:r => badge(r.status, r.status === 'Done' ? 'green' : r.status === 'In Progress' ? 'amber' : r.status === 'Planned' ? 'blue' : 'gray') },
            { key:'effort', label:'Effort', sortable:false, render:r => r.effort ? esc(r.effort) : dash },
            { key:'assigned_team', label:'Team', sortable:false, render:r => r.assigned_team ? esc(r.assigned_team) : '<span class="faint">unassigned</span>' },
            { key:'target_release', label:'Target', sortable:false, render:r => r.target_release ? esc(r.target_release) : dash },
            { key:'updated_at', label:'Updated', render:r => when(r.updated_at) },
          ],
          rowId:'id', pageSize:10, itemName:'backlog items', searchPlaceholder:'Search backlog…',
          defaultSort:{ key:'updated_at', dir:-1 },
          emptyText:'The improvement backlog is empty',
          filters:[
            { key:'status', label:'Status', param:'status', options:BACKLOG_STATUS, allLabel:'All Status' },
            { key:'priority', label:'Priority', param:'priority', options:BACKLOG_PRIORITY, allLabel:'All Priorities' },
          ],
          source: (params) => API.feedback.backlog.list(params),
          rowActions: r => [
            { label:'Create Fix Task', icon:'tool', onClick:()=>openFixTask(r, ()=>t.refresh()) },
            ...(r.status !== 'Done' ? [{ label:'Move to ' + nextStatus(r.status), icon:'arrowRight', onClick:()=>moveBacklog(r, nextStatus(r.status), ()=>t.refresh()) }] : []),
            { sep:true },
            // The API refuses a jump to Done from Backlog or Planned.
            ...(r.status === 'In Progress' ? [{ label:'Mark Deployed', icon:'rocket', onClick:()=>moveBacklog(r, 'Done', ()=>t.refresh()) }] : []),
          ],
        });
        const holder = document.getElementById('fbBacklogTbl');
        holder.appendChild(t.filterEl);
        holder.appendChild(t.el);
      }

      function nextStatus(s){
        const order = BACKLOG_STATUS;
        const i = order.indexOf(s);
        return i < 0 || i === order.length - 1 ? 'Done' : order[i + 1];
      }

      function tabActions(){
        // Only the clustering pass has an endpoint; the rest are advertised but
        // unbacked, so they are shown disabled rather than faked.
        const ACTIONS = [
          { label:'Analyze negative feedback (last 7d)', icon:'beaker', desc:'Cluster and summarise new negative feedback', run:()=>openAnalyze({ window_days:7, negative_only:true }) },
          { label:'Notify agent owners', icon:'send', desc:'Send the weekly quality digest to owners', why:'No notification endpoint exists yet.' },
          { label:'Sync issues to the tracker', icon:'external', desc:'Push open issues to the issue tracker', why:'No issue-tracker integration endpoint exists yet.' },
          { label:'Retrain FAQ intents', icon:'refresh', desc:'Refresh intent examples from feedback', why:'No retraining endpoint exists yet.' },
        ];
        body.innerHTML = `<div class="grid g2">${ACTIONS.map((a, k) => `
          <div class="card flex between"><div class="flex" style="gap:10px">
            <span class="kpi-ico" style="background:var(--purple-dim);color:var(--purple-bright)">${ICONS[a.icon]}</span>
            <div><b>${esc(a.label)}</b><div class="small dim">${esc(a.desc)}</div></div></div>
            <button class="btn sm ${a.run ? 'primary' : ''}" data-act="${k}"${a.run ? gate('member','Analyzing feedback requires the member role.') : ` disabled title="${esc(a.why)}"`}>Run</button></div>`).join('')}
          </div>
          <div class="card mt"><div class="card-head"><div class="card-title">Clustering</div></div>
            <div class="small dim">Analysis is deterministic: the same window analysed twice produces the same clusters, so an issue title suggested here can be reproduced.</div>
            <button class="btn sm primary mt" id="fbAnalyzeAll"${gate('member','Analyzing feedback requires the member role.')}>${ICONS.beaker}Analyze all feedback (30d)</button></div>`;
        body.querySelectorAll('[data-act]').forEach(b => {
          const a = ACTIONS[b.dataset.act];
          if(a.run) b.addEventListener('click', a.run);
        });
        document.getElementById('fbAnalyzeAll').addEventListener('click', ()=>openAnalyze({ window_days:30 }));
      }

      function tabReports(){
        body.innerHTML = `<div class="grid g3">
            <div class="card"><div class="flex" style="gap:10px">
              <span class="kpi-ico" style="background:var(--green-dim);color:#15803D">${ICONS.fileText}</span>
              <div class="grow"><b>Feedback Extract</b><div class="small dim">CSV · the filtered feedback table</div></div></div>
              <button class="btn sm mt" id="rpFeedback">${ICONS.download}Download CSV</button></div>
            <div class="card"><div class="flex" style="gap:10px">
              <span class="kpi-ico" style="background:var(--blue-dim);color:#1D4ED8">${ICONS.fileText}</span>
              <div class="grow"><b>Weekly Quality Report</b><div class="small dim">PDF · scheduled document</div></div></div>
              <button class="btn sm mt" disabled title="No report generator endpoint exists. Build this as a scheduled export instead.">${ICONS.download}Download</button></div>
            <div class="card"><div class="flex" style="gap:10px">
              <span class="kpi-ico" style="background:var(--blue-dim);color:#1D4ED8">${ICONS.fileText}</span>
              <div class="grow"><b>Agent Quality Scorecards</b><div class="small dim">XLSX · per-agent rollup</div></div></div>
              <button class="btn sm mt" disabled title="No report generator endpoint exists. Build this as a scheduled export instead.">${ICONS.download}Download</button></div>
          </div>
          <div class="quote mt">${ICONS.info} Recurring documents are generated on the
            <span class="link" data-nav="exports">Exports</span> screen, which schedules and retains them.</div>`;
        document.getElementById('rpFeedback').addEventListener('click', async () => {
          try { await API.feedback.export({ window_days: 30 }); toast('success','Export complete','Feedback exported to CSV.'); }
          catch (err) { toast('error','Export failed', errText(err)); }
        });
      }

      function tabSettings(){
        body.innerHTML = `<div class="grid g2">
          <div class="card" id="fbSetCard"><div class="card-loading" style="height:180px"></div></div>
          <div class="card" id="fbSlaCard"><div class="card-loading" style="height:180px"></div></div></div>`;
        loadCollectionSettings();
        loadSlaRules();
      }

      function loadCollectionSettings(){
        const host = document.getElementById('fbSetCard');
        if(!host) return;
        API.feedback.settings()
          .then(s => {
            const el = document.getElementById('fbSetCard');
            if(!el) return;
            el.innerHTML = `<div class="card-head"><div class="card-title">Collection Settings</div></div>
              ${kv([
                ['In-app rating prompt', s.in_app_rating_prompt ? `Enabled (${esc(s.in_app_rating_trigger)})` : '<span class="faint">Disabled</span>'],
                ['Thumbs up/down on responses', s.response_thumbs ? '<span class="st-green">Enabled</span>' : '<span class="faint">Disabled</span>'],
                ['Support ticket ingestion', s.support_ticket_ingestion ? `Enabled${s.support_ticket_system ? ' (' + esc(s.support_ticket_system) + ')' : ''}` : '<span class="faint">Disabled</span>'],
                ['Manual review sampling', `${s.manual_review_sample_percent}% of runs`],
                ['PII scrubbing on feedback', s.pii_scrubbing ? '<span class="st-green">Enabled</span>' : '<span class="st-red">Disabled</span>'],
              ])}
              <button class="btn sm mt" id="fbSetEdit"${gate('admin','Editing collection settings requires the admin role.')}>${ICONS.settings}Edit Settings</button>`;
            document.getElementById('fbSetEdit').addEventListener('click', ()=>editSettings(s));
          })
          .catch(err => {
            const el = document.getElementById('fbSetCard');
            if(el){ el.innerHTML = ''; el.appendChild(screenError(err, loadCollectionSettings, 'the collection settings')); }
          });
      }

      function editSettings(s){
        if(!allowed('admin','Editing collection settings requires the admin role.')) return;
        openModal({
          title:'Collection Settings', icon:'settings',
          body:`<div class="form-row"><label>IN-APP RATING PROMPT</label>
              <select class="filter-select w-100" id="csPrompt" style="height:34px">
                <option value="true" ${s.in_app_rating_prompt ? 'selected' : ''}>Enabled</option>
                <option value="false" ${s.in_app_rating_prompt ? '' : 'selected'}>Disabled</option></select></div>
            <div class="form-row"><label>PROMPT TRIGGER</label><input class="input" id="csTrigger" value="${esc(s.in_app_rating_trigger)}"></div>
            <div class="grid g2">
              <div class="form-row"><label>RESPONSE THUMBS</label>
                <select class="filter-select w-100" id="csThumbs" style="height:34px">
                  <option value="true" ${s.response_thumbs ? 'selected' : ''}>Enabled</option>
                  <option value="false" ${s.response_thumbs ? '' : 'selected'}>Disabled</option></select></div>
              <div class="form-row"><label>PII SCRUBBING</label>
                <select class="filter-select w-100" id="csPii" style="height:34px">
                  <option value="true" ${s.pii_scrubbing ? 'selected' : ''}>Enabled</option>
                  <option value="false" ${s.pii_scrubbing ? '' : 'selected'}>Disabled</option></select></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>SUPPORT TICKET INGESTION</label>
                <select class="filter-select w-100" id="csTickets" style="height:34px">
                  <option value="true" ${s.support_ticket_ingestion ? 'selected' : ''}>Enabled</option>
                  <option value="false" ${s.support_ticket_ingestion ? '' : 'selected'}>Disabled</option></select></div>
              <div class="form-row"><label>MANUAL REVIEW SAMPLE %</label>
                <input class="input" id="csSample" type="number" min="0" max="100" step="0.5" value="${s.manual_review_sample_percent}"></div>
            </div>
            <div class="form-row"><label>TICKET SYSTEM</label><input class="input" id="csSystem" value="${esc(s.support_ticket_system || '')}" placeholder="e.g. the support desk in use"></div>`,
          footer:[{ label:'Cancel' }, { label:'Save Settings', cls:'primary', onClick: async (close, modal) => {
            const b = {
              in_app_rating_prompt: modal.querySelector('#csPrompt').value === 'true',
              in_app_rating_trigger: modal.querySelector('#csTrigger').value.trim() || 'After each session',
              response_thumbs: modal.querySelector('#csThumbs').value === 'true',
              support_ticket_ingestion: modal.querySelector('#csTickets').value === 'true',
              support_ticket_system: modal.querySelector('#csSystem').value.trim() || null,
              manual_review_sample_percent: parseFloat(modal.querySelector('#csSample').value) || 0,
              pii_scrubbing: modal.querySelector('#csPii').value === 'true',
            };
            close();
            try {
              await Store.mutate(() => API.feedback.saveSettings(b), { event:'feedback:changed' });
              toast('success','Settings saved','Feedback collection now follows the new rules.');
              loadCollectionSettings();
            } catch (err) { toast('error','Could not save the settings', errText(err)); }
          } }],
        });
      }

      function loadSlaRules(){
        const host = document.getElementById('fbSlaCard');
        if(!host) return;
        API.feedback.slaRules()
          .then(s => {
            const el = document.getElementById('fbSlaCard');
            if(!el) return;
            el.innerHTML = `<div class="card-head"><div class="card-title">SLA &amp; Routing</div></div>
              ${kv([
                ['Negative feedback triage SLA', `${s.triage_sla_hours} ${s.business_hours_only ? 'business' : 'clock'} hours`],
                ['Business day', s.business_hours_only ? `${s.business_day_start_hour}:00 – ${s.business_day_end_hour}:00` : '<span class="faint">24 hours</span>'],
                ['Issue creation', `Auto for ≥ ${s.auto_issue_threshold} similar reports`],
                ['Routing', esc(s.routing) + (s.routing_team ? ` · ${esc(s.routing_team)}` : '')],
                ['Escalation', `${esc(s.escalation_contact)} after ${s.escalate_after_hours}h`],
                ['Severity windows', `C ${s.severity_sla.critical}h · H ${s.severity_sla.high}h · M ${s.severity_sla.medium}h · L ${s.severity_sla.low}h`],
              ])}
              <button class="btn sm mt" id="fbSlaEdit"${gate('admin','Editing the SLA rules requires the admin role.')}>${ICONS.edit}Edit SLA Rules</button>`;
            document.getElementById('fbSlaEdit').addEventListener('click', ()=>editSla(s));
          })
          .catch(err => {
            const el = document.getElementById('fbSlaCard');
            if(el){ el.innerHTML = ''; el.appendChild(screenError(err, loadSlaRules, 'the SLA rules')); }
          });
      }

      function editSla(s){
        if(!allowed('admin','Editing the SLA rules requires the admin role.')) return;
        openModal({
          title:'Edit SLA Rules', icon:'edit',
          body:`<div class="grid g2">
              <div class="form-row"><label>TRIAGE SLA (HOURS)</label><input class="input" id="slHours" type="number" min="1" value="${s.triage_sla_hours}"></div>
              <div class="form-row"><label>AUTO-ISSUE THRESHOLD</label><input class="input" id="slThreshold" type="number" min="2" value="${s.auto_issue_threshold}"></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>BUSINESS HOURS ONLY</label><select class="filter-select w-100" id="slBiz" style="height:34px">
                <option value="true" ${s.business_hours_only ? 'selected' : ''}>Yes</option>
                <option value="false" ${s.business_hours_only ? '' : 'selected'}>No</option></select></div>
              <div class="form-row"><label>ROUTING</label><select class="filter-select w-100" id="slRouting" style="height:34px">
                <option ${s.routing === 'Agent owner team' ? 'selected' : ''}>Agent owner team</option>
                <option ${s.routing === 'Fixed team' ? 'selected' : ''}>Fixed team</option></select></div>
            </div>
            <div class="form-row"><label>FIXED TEAM (REQUIRED WHEN ROUTING IS FIXED)</label><input class="input" id="slTeam" value="${esc(s.routing_team || '')}"></div>
            <div class="grid g2">
              <div class="form-row"><label>ESCALATION CONTACT</label><input class="input" id="slContact" value="${esc(s.escalation_contact)}"></div>
              <div class="form-row"><label>ESCALATE AFTER (HOURS)</label><input class="input" id="slEsc" type="number" min="1" value="${s.escalate_after_hours}"></div>
            </div>
            <div class="small muted" style="font-weight:700;margin:8px 0 4px">SEVERITY WINDOWS (HOURS) — MUST WIDEN AS SEVERITY FALLS</div>
            <div class="grid g4">
              ${[['critical','Critical'],['high','High'],['medium','Medium'],['low','Low']].map(([k, l]) =>
                `<div class="form-row"><label>${l}</label><input class="input" id="sl_${k}" type="number" min="1" value="${s.severity_sla[k]}"></div>`).join('')}
            </div>`,
          footer:[{ label:'Cancel' }, { label:'Save Rules', cls:'primary', onClick: async (close, modal) => {
            const b = {
              triage_sla_hours: parseInt(modal.querySelector('#slHours').value, 10),
              business_hours_only: modal.querySelector('#slBiz').value === 'true',
              business_day_start_hour: s.business_day_start_hour,
              business_day_end_hour: s.business_day_end_hour,
              auto_issue_threshold: parseInt(modal.querySelector('#slThreshold').value, 10),
              routing: modal.querySelector('#slRouting').value,
              routing_team: modal.querySelector('#slTeam').value.trim() || null,
              escalation_contact: modal.querySelector('#slContact').value.trim(),
              escalate_after_hours: parseInt(modal.querySelector('#slEsc').value, 10),
              severity_sla: {
                critical: parseInt(modal.querySelector('#sl_critical').value, 10),
                high: parseInt(modal.querySelector('#sl_high').value, 10),
                medium: parseInt(modal.querySelector('#sl_medium').value, 10),
                low: parseInt(modal.querySelector('#sl_low').value, 10),
              },
            };
            close();
            try {
              await Store.mutate(() => API.feedback.saveSlaRules(b), { event:'feedback:changed' });
              toast('success','SLA rules saved','The triage clock now runs on the new windows.');
              loadSlaRules();
              loadSummary();
            } catch (err) { toast('error','Could not save the SLA rules', errText(err)); }
          } }],
        });
      }

      // ---- feedback actions ---------------------------------------------------
      function openCreateIssue(seed, after){
        if(!allowed('member','Creating an issue requires the member role.')) return;
        openModal({
          title:'Create Issue', icon:'bug',
          body:`<div class="form-row"><label>TITLE</label><input class="input" id="ciTitle" value="${esc(seed.title || '')}" placeholder="What is going wrong?"></div>
            <div class="form-row"><label>DESCRIPTION</label><textarea class="input" id="ciDesc" rows="3" placeholder="Optional detail for the owning team"></textarea></div>
            <div class="grid g2">
              <div class="form-row"><label>SEVERITY</label><select class="filter-select w-100" id="ciSev" style="height:34px">
                ${ISSUE_SEVERITY.map(x => `<option ${x === (seed.severity || 'Medium') ? 'selected' : ''}>${x}</option>`).join('')}</select></div>
              <div class="form-row"><label>ASSIGN TO TEAM (OPTIONAL)</label><input class="input" id="ciTeam" placeholder="Leave blank to route by rule"></div>
            </div>
            ${seed.theme ? `<div class="quote">Theme <b>${esc(seed.theme)}</b> — every unlinked item in it will be attached to this issue.</div>` : ''}`,
          footer:[{ label:'Cancel' }, { label:'Create Issue', cls:'primary', onClick: async (close, modal) => {
            const title = modal.querySelector('#ciTitle').value.trim();
            if(!title){ toast('error','Title required','Give the issue a title.'); return; }
            const b = { title, severity: modal.querySelector('#ciSev').value };
            const desc = modal.querySelector('#ciDesc').value.trim();
            if(desc) b.description = desc;
            const team = modal.querySelector('#ciTeam').value.trim();
            if(team) b.assigned_team = team;
            if(seed.feedback_ids) b.feedback_ids = seed.feedback_ids;
            if(seed.agent_id) b.agent_id = seed.agent_id;
            if(seed.theme) b.theme = seed.theme;
            if(seed.cluster_id) b.cluster_id = seed.cluster_id;
            close();
            try {
              const issue = await Store.mutate(() => API.feedback.createIssue(b), { event:'feedback:changed' });
              toast('success','Issue created', `${issue.issue_ref} · ${issue.feedback_count} report${issue.feedback_count === 1 ? '' : 's'} linked, routed to ${issue.assigned_team || 'the default team'}.`);
              if(mainTable) mainTable.refresh();
              loadSummary();
              if(after) after(issue);
            } catch (err) {
              toast('error','Could not create the issue', errText(err));
            }
          } }],
        });
      }

      function openAddBacklog(r){
        if(!allowed('member','Adding to the backlog requires the member role.')) return;
        openModal({
          title:'Add to Improvement Backlog', icon:'plus',
          body:`<div class="form-row"><label>TITLE</label><input class="input" id="abTitle" value="${esc((r.body || '').slice(0,80))}" placeholder="What should change?"></div>
            <div class="grid g2">
              <div class="form-row"><label>PRIORITY</label><select class="filter-select w-100" id="abPri" style="height:34px">
                ${BACKLOG_PRIORITY.map(p => `<option ${p === 'P2' ? 'selected' : ''}>${p}</option>`).join('')}</select></div>
              <div class="form-row"><label>EFFORT (OPTIONAL)</label><input class="input" id="abEffort" placeholder="e.g. 3d"></div>
            </div>
            <div class="form-row"><label>TEAM (OPTIONAL)</label><input class="input" id="abTeam"></div>
            ${r.issue_ref ? `<div class="quote">Linked to issue <b>${esc(r.issue_ref)}</b>.</div>` : `<div class="quote">${ICONS.info} This item has no issue behind it yet, so the backlog entry stands alone.</div>`}`,
          footer:[{ label:'Cancel' }, { label:'Add to Backlog', cls:'primary', onClick: async (close, modal) => {
            const title = modal.querySelector('#abTitle').value.trim();
            if(!title){ toast('error','Title required','Give the backlog item a title.'); return; }
            const b = { title, priority: modal.querySelector('#abPri').value, feedback_id: r.id };
            const effort = modal.querySelector('#abEffort').value.trim();
            if(effort) b.effort = effort;
            const team = modal.querySelector('#abTeam').value.trim();
            if(team) b.assigned_team = team;
            close();
            try {
              const item = await Store.mutate(() => API.feedback.backlog.create(b), { event:'feedback:changed' });
              toast('success','Added to backlog', `${item.title} · ${item.priority}${item.votes ? ` · ${item.votes} reports behind it` : ''}.`);
              loadSummary();
              if(activeTab === 4) switchTab(4);
            } catch (err) {
              toast('error','Could not add to the backlog', errText(err));
            }
          } }],
        });
      }

      function openAssignTeam(r){
        if(!allowed('member','Assigning a team requires the member role.')) return;
        if(!r.issue_id){
          confirmModal({
            title:'No issue to route', icon:'users', confirmLabel:'Create Issue',
            msg:'Assignment routes the issue this report belongs to, and this report is not linked to one yet. Create an issue for it now?',
            onConfirm: ()=>openCreateIssue({ feedback_ids:[r.id], title:(r.body || '').slice(0,80), agent_id:r.agent_id, theme:r.theme }),
          });
          return;
        }
        openModal({
          title:'Assign to Team', icon:'users',
          body:`<div class="quote">Issue <b>${esc(r.issue_ref || r.issue_id)}</b>${r.issue_title ? ` — ${esc(r.issue_title)}` : ''}</div>
            <div class="form-row mt"><label>TEAM</label><input class="input" id="atTeam" placeholder="e.g. Document Intelligence"></div>`,
          footer:[{ label:'Cancel' }, { label:'Assign', cls:'primary', onClick: async (close, modal) => {
            const team = modal.querySelector('#atTeam').value.trim();
            if(!team){ toast('error','Team required','Name the team that will own this.'); return; }
            close();
            try {
              const issue = await Store.mutate(() => API.feedback.issues.update(r.issue_id, { assigned_team: team }), { event:'feedback:changed' });
              toast('success','Assigned', `${issue.issue_ref} routed to ${issue.assigned_team}.`);
              if(mainTable) mainTable.refresh();
            } catch (err) { toast('error','Could not assign', errText(err)); }
          } }],
        });
      }

      function openAssignIssue(r, after){
        if(!allowed('member','Assigning an issue requires the member role.')) return;
        openModal({
          title:'Assign Issue — ' + r.issue_ref, icon:'users',
          body:`<div class="form-row"><label>TEAM</label><input class="input" id="aiTeam" value="${esc(r.assigned_team || '')}" placeholder="e.g. Document Intelligence"></div>
            <div class="form-row"><label>SEVERITY</label><select class="filter-select w-100" id="aiSev" style="height:34px">
              ${ISSUE_SEVERITY.map(x => `<option ${x === r.severity ? 'selected' : ''}>${x}</option>`).join('')}</select></div>
            <div class="quote">${ICONS.info} Re-grading an open issue recomputes its SLA due date.</div>`,
          footer:[{ label:'Cancel' }, { label:'Save', cls:'primary', onClick: async (close, modal) => {
            const b = { assigned_team: modal.querySelector('#aiTeam').value.trim() || null, severity: modal.querySelector('#aiSev').value };
            close();
            try {
              const issue = await Store.mutate(() => API.feedback.issues.update(r.id, b), { event:'feedback:changed' });
              toast('success','Issue updated', `${issue.issue_ref} is ${issue.severity}, owned by ${issue.assigned_team || 'the default team'}.`);
              after();
              loadSummary();
            } catch (err) { toast('error','Could not update the issue', errText(err)); }
          } }],
        });
      }

      function resolveIssue(r, after){
        if(!allowed('member','Resolving an issue requires the member role.')) return;
        openModal({
          title:'Resolve Issue — ' + r.issue_ref, icon:'checkCircle',
          body:`<div class="form-row"><label>RESOLUTION NOTE</label><textarea class="input" id="riNote" rows="3" placeholder="What changed?"></textarea></div>
            <div class="quote">${ICONS.info} Resolving freezes whether the SLA was met.</div>`,
          footer:[{ label:'Cancel' }, { label:'Resolve', cls:'primary', onClick: async (close, modal) => {
            const b = { status:'Resolved' };
            const note = modal.querySelector('#riNote').value.trim();
            if(note) b.resolution_note = note;
            close();
            try {
              const issue = await Store.mutate(() => API.feedback.issues.update(r.id, b), { event:'feedback:changed' });
              toast('success','Issue resolved', `${issue.issue_ref} closed${issue.sla_met == null ? '' : issue.sla_met ? ' within SLA.' : ' past its SLA.'}`);
              after();
              loadSummary();
            } catch (err) { toast('error','Could not resolve the issue', errText(err)); }
          } }],
        });
      }

      function promoteIssue(r, after){
        if(!allowed('member','Planning an issue requires the member role.')) return;
        openModal({
          title:'Add to Backlog — ' + r.issue_ref, icon:'plus',
          body:`<div class="grid g2">
              <div class="form-row"><label>PRIORITY</label><select class="filter-select w-100" id="piPri" style="height:34px">
                <option value="">Derive from severity (${esc(r.severity)})</option>
                ${BACKLOG_PRIORITY.map(p => `<option>${p}</option>`).join('')}</select></div>
              <div class="form-row"><label>EFFORT (OPTIONAL)</label><input class="input" id="piEffort" placeholder="e.g. 3d"></div>
            </div>
            <div class="form-row"><label>TARGET RELEASE (OPTIONAL)</label><input class="input" id="piRelease" placeholder="e.g. 2026.09"></div>
            <div class="quote">${ICONS.info} The issue moves to In Progress once it is planned.</div>`,
          footer:[{ label:'Cancel' }, { label:'Add to Backlog', cls:'primary', onClick: async (close, modal) => {
            const b = {};
            const pri = modal.querySelector('#piPri').value;
            if(pri) b.priority = pri;
            const effort = modal.querySelector('#piEffort').value.trim();
            if(effort) b.effort = effort;
            const rel = modal.querySelector('#piRelease').value.trim();
            if(rel) b.target_release = rel;
            close();
            try {
              const item = await Store.mutate(() => API.feedback.issueToBacklog(r.id, b), { event:'feedback:changed' });
              toast('success','Planned as improvement', `${item.title} · ${item.priority} · ${item.votes} reports behind it.`);
              after();
              loadSummary();
            } catch (err) { toast('error','Could not plan this issue', errText(err)); }
          } }],
        });
      }

      function openFixTaskFromIssue(r, after){
        if(!allowed('member','Creating a fix task requires the member role.')) return;
        if(!r.backlog_item_id){
          confirmModal({
            title:'Not on the backlog yet', icon:'tool', confirmLabel:'Add to Backlog',
            msg:'A fix task tracks a backlog item to a measured outcome, and this issue has not been planned yet. Add it to the backlog first?',
            onConfirm: ()=>promoteIssue(r, after),
          });
          return;
        }
        openFixTask({ id: r.backlog_item_id, title: r.title, assigned_team: r.assigned_team }, after);
      }

      function openFixTask(item, after){
        if(!allowed('member','Creating a fix task requires the member role.')) return;
        openModal({
          title:'Create Fix Task', icon:'tool',
          body:`<div class="quote"><b>${esc(item.title)}</b><div class="small dim">The metric is captured as it stands now, so the before/after published when the fix ships is a measurement rather than a recollection.</div></div>
            <div class="grid g2">
              <div class="form-row"><label>TEAM</label><input class="input" id="ftTeam" value="${esc(item.assigned_team || '')}"></div>
              <div class="form-row"><label>TARGET RELEASE</label><input class="input" id="ftRelease" placeholder="e.g. 2026.09"></div>
            </div>
            <div class="grid g2">
              <div class="form-row"><label>METRIC (HIGHER IS BETTER)</label><input class="input" id="ftMetric" placeholder="e.g. extraction accuracy %"></div>
              <div class="form-row"><label>CURRENT VALUE</label><input class="input" id="ftBefore" type="number" step="0.01" placeholder="e.g. 87.4"></div>
            </div>
            <div class="form-row"><label>NOTE (OPTIONAL)</label><textarea class="input" id="ftNote" rows="2"></textarea></div>`,
          footer:[{ label:'Cancel' }, { label:'Create Fix Task', cls:'primary', onClick: async (close, modal) => {
            const b = {};
            const team = modal.querySelector('#ftTeam').value.trim();
            if(team) b.assigned_team = team;
            const rel = modal.querySelector('#ftRelease').value.trim();
            if(rel) b.target_release = rel;
            const metric = modal.querySelector('#ftMetric').value.trim();
            if(metric) b.metric_name = metric;
            const before = modal.querySelector('#ftBefore').value;
            if(before !== '') b.before_metric = parseFloat(before);
            const note = modal.querySelector('#ftNote').value.trim();
            if(note) b.note = note;
            close();
            try {
              const imp = await Store.mutate(() => API.feedback.fixTask(item.id, b), { event:'feedback:changed' });
              toast('success','Fix task created', `${imp.title} is In Progress${imp.metric_name ? ` against ${imp.metric_name}` : ''}.`);
              after();
              loadSummary();
            } catch (err) { toast('error','Could not create the fix task', errText(err)); }
          } }],
        });
      }

      function moveBacklog(r, status, after){
        if(!allowed('member','Moving a backlog item requires the member role.')) return;
        const isDeploy = status === 'Done';
        openModal({
          title: isDeploy ? 'Mark Deployed — ' + r.title : `Move to ${status}`, icon: isDeploy ? 'rocket' : 'arrowRight',
          body: isDeploy
            ? `<div class="quote">Marking this deployed stamps the fix task with the moment it shipped and the measured after-value. The improvement counts as verified only when both sides of the metric are present and it moved.</div>
               <div class="form-row mt"><label>MEASURED VALUE AFTER THE FIX</label><input class="input" id="mbAfter" type="number" step="0.01" placeholder="Leave blank if not measured yet"></div>
               <div class="form-row"><label>IMPACT SUMMARY</label><textarea class="input" id="mbImpact" rows="2" placeholder="What changed for users?"></textarea></div>`
            : `<p style="margin:0">Move <b>${esc(r.title)}</b> from ${esc(r.status)} to ${esc(status)}?</p>`,
          footer:[{ label:'Cancel' }, { label: isDeploy ? 'Mark Deployed' : 'Move', cls:'primary', onClick: async (close, modal) => {
            const b = { status };
            if(isDeploy){
              const after_ = modal.querySelector('#mbAfter').value;
              if(after_ !== '') b.after_metric = parseFloat(after_);
              const impact = modal.querySelector('#mbImpact').value.trim();
              if(impact) b.impact_summary = impact;
            }
            close();
            try {
              const item = await Store.mutate(() => API.feedback.backlog.update(r.id, b), { event:'feedback:changed' });
              toast('success', isDeploy ? 'Improvement deployed' : 'Backlog item moved', `${item.title} is now ${item.status}.`);
              after();
              loadSummary();
            } catch (err) { toast('error','Could not move the item', errText(err)); }
          } }],
        });
      }

      /** The clustering pass, and the clusters it actually found. */
      function openAnalyze(opts){
        if(!allowed('member','Analyzing feedback requires the member role.')) return;
        const req = Object.assign({ window_days: 30, min_cluster_size: 2, recluster: false, negative_only: false }, opts || {});
        openModal({
          title:'Analyze Feedback', icon:'beaker', wide:true,
          body:`<div class="card-loading" style="height:200px"></div>`,
          footer:[{ label:'Close' }],
          onOpen(modal){
            const body_ = modal.querySelector('.modal-body');
            API.feedback.analyze(req)
              .then(res => {
                const clusters = res.clusters || [];
                body_.innerHTML = `
                  <div class="flex between" style="margin-bottom:10px">
                    <div><b>${fmtFull(res.analysed)} item${res.analysed === 1 ? '' : 's'} analysed</b>
                      <div class="small dim">${fmtFull(res.clustered)} clustered · ${fmtFull(res.unclustered)} left unclustered · last ${res.window_days} days · similarity ${res.similarity}</div></div>
                    ${badge(`${clusters.length} theme${clusters.length === 1 ? '' : 's'}`, clusters.length ? 'purple' : 'gray')}</div>
                  ${clusters.length ? clusters.map((c, i) => `
                    <div class="card mb" style="padding:12px">
                      <div class="flex between">
                        <div><b>${esc(c.theme)}</b>
                          <div class="small dim">${fmtFull(c.size)} item${c.size === 1 ? '' : 's'} · ${(c.negative_share * 100).toFixed(0)}% negative
                            · avg rating ${c.avg_rating == null ? '—' : c.avg_rating.toFixed(2)}
                            ${c.agents.length ? ' · ' + esc(c.agents.join(', ')) : ''}</div></div>
                        <div class="flex" style="gap:6px">${riskBadge(c.suggested_severity)}
                          ${c.meets_auto_issue_threshold ? badge('Meets auto-issue threshold','amber') : ''}</div></div>
                      ${c.keywords.length ? `<div style="margin-top:6px">${c.keywords.map(k => `<span class="tag">${esc(k)}</span>`).join('')}</div>` : ''}
                      <div class="small muted" style="font-weight:700;margin:8px 0 3px">SUGGESTED ISSUE</div>
                      <div class="quote">${esc(c.suggested_issue_title)}</div>
                      <div class="small muted" style="font-weight:700;margin:8px 0 3px">REPRESENTATIVE EXAMPLES</div>
                      ${(c.examples || []).map(x => `<div class="quote small"><b>${esc(x.feedback_ref)}</b>
                        ${x.rating == null ? '' : ' · ' + x.rating + '★'} · ${esc(x.sentiment)}${x.agent_name ? ' · ' + esc(x.agent_name) : ''}
                        <div class="dim" style="margin-top:3px">${esc(x.body || 'no comment')}</div></div>`).join('')
                        || '<div class="faint small">No example was quoted back.</div>'}
                      <button class="btn sm primary mt" data-cluster="${i}">${ICONS.bug}Create Issue from this theme</button>
                    </div>`).join('')
                    : emptyBlock('layers','The pass found no cluster in this window',
                        `${fmtFull(res.analysed)} item${res.analysed === 1 ? ' was' : 's were'} examined; none were similar enough to group at the current threshold.`)}`;
                body_.querySelectorAll('[data-cluster]').forEach(btn => {
                  btn.addEventListener('click', () => {
                    const c = clusters[btn.dataset.cluster];
                    openCreateIssue({ title: c.suggested_issue_title, severity: c.suggested_severity, theme: c.theme, cluster_id: c.cluster_id });
                  });
                });
                if(mainTable) mainTable.refresh();
                loadSummary();
              })
              .catch(err => { body_.innerHTML = ''; body_.appendChild(screenError(err, null, 'the clustering pass')); });
          },
        });
      }

      // ---- header controls -----------------------------------------------------
      document.getElementById('fbSearch').addEventListener('input', e => { if(mainTable) mainTable.search(e.target.value); });
      document.getElementById('fbExport').addEventListener('click', () => {
        if(mainTable) mainTable.export();
        else API.feedback.export({ window_days: 30 })
          .then(()=>toast('success','Export complete','Feedback exported to CSV.'))
          .catch(err=>toast('error','Export failed', errText(err)));
      });
      document.getElementById('fbSubmit').addEventListener('click', async () => {
        if(!allowed('member','Submitting feedback requires the member role.')) return;
        let agents = { items: [] };
        try { agents = await API.agents.list({ page_size: 100 }); } catch (_) { /* the picker degrades to none */ }
        openModal({
          title:'Submit Feedback', icon:'chat',
          body:`<div class="form-row"><label>AGENT</label><select class="filter-select w-100" id="nfAgent" style="height:34px">
              <option value="">Not about a specific agent</option>
              ${(agents.items || []).map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('')}</select></div>
            <div class="grid g2">
              <div class="form-row"><label>RATING</label><select class="filter-select w-100" id="nfRating" style="height:34px">
                ${[[5,'5 — Excellent'],[4,'4 — Good'],[3,'3 — OK'],[2,'2 — Poor'],[1,'1 — Bad'],['','No rating']].map(([v, l]) =>
                  `<option value="${v}" ${v === 5 ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
              <div class="form-row"><label>SOURCE</label><select class="filter-select w-100" id="nfSource" style="height:34px">
                ${FB_SOURCES.map(s => `<option ${s === 'Manual Review' ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select></div>
            </div>
            <div class="form-row"><label>RUN / TRACE ID (OPTIONAL)</label><input class="input" id="nfTrace" placeholder="Links the rating onto the trace"></div>
            <div class="form-row"><label>FEEDBACK</label><textarea class="input" id="nfText" rows="3" placeholder="Describe the experience…"></textarea></div>`,
          footer:[{ label:'Cancel' }, { label:'Submit', cls:'primary', onClick: async (close, modal) => {
            const rating = modal.querySelector('#nfRating').value;
            const text = modal.querySelector('#nfText').value.trim();
            // The API 422s a body with neither ("feedback needs a rating, a
            // comment, or both"), so say so before the request is made.
            if(!rating && !text){ toast('error','Comment required','Feedback without a rating needs a comment to carry it.'); return; }
            const b = { source: modal.querySelector('#nfSource').value };
            if(rating) b.rating = parseInt(rating, 10);
            const agentId = modal.querySelector('#nfAgent').value;
            if(agentId) b.agent_id = agentId;
            const trace = modal.querySelector('#nfTrace').value.trim();
            if(trace) b.trace_id = trace;
            if(text) b.body = text;
            close();
            try {
              const created = await Store.mutate(() => API.feedback.create(b), { event:'feedback:changed' });
              toast('success','Feedback submitted',
                `${created.feedback_ref} recorded as ${created.sentiment}${created.scored_in_telemetry ? ' and scored onto the trace.' : '.'}`);
              if(mainTable) mainTable.refresh();
              loadSummary();
            } catch (err) {
              toast('error','Could not submit the feedback', errText(err));
            }
          } }],
        });
      });

      loadSummary(()=>{});
      renderTab(0);
    },
  };
})();
