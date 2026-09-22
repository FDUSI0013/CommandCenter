/* FD AI Command Center — PLATFORM screens: Live Runs, Replay Studio, Metrics
 *
 * Everything here is telemetry: it comes from the platform's run and metric
 * endpoints, which read the engine behind them. Nothing on these screens
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

  /* A connection's kind decides its mark when the server did not name one.
     The same map lives on the Connection Center; it is repeated here rather
     than shared because icons.js holds the marks, not the vocabulary. */
  const KIND_LOGO = { 'Azure AI Foundry':'foundry', 'Copilot Studio':'copilot', 'M365 Copilot':'m365',
    'Microsoft 365':'m365', 'Power Platform':'power', 'MCP Server':'mcp', 'Vector Database':'vector',
    'Microsoft Purview':'purview', 'Custom REST API':'custom', 'Azure Key Vault':'keyvault',
    'SharePoint':'sharepoint', 'SQL Database':'sql', 'Azure Blob Storage':'azureblob' };

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

  /**
   * What a run recorded about ITSELF, as opposed to on its spans: its input,
   * output, error and metadata, which the API reads off the trace.
   *
   * A run reported as one decorated call has no spans at all, and these four
   * are then the whole account of it. The trace modal and Replay Studio used to
   * draw such a run as an empty shell ("0 of 0 steps", "No spans were
   * recorded") while the API held every word of it — and for one customer that
   * is every run they have. Nothing here is a span and the callers label it as
   * the run's own record; a field that was not recorded is left out, never
   * filled in. `skip` names the fields the caller already shows in full.
   */
  function runRecord(rec, skip){
    rec = rec || {}; skip = skip || {};
    const label = (text, first) =>
      `<div class="small muted" style="font-weight:700;margin:${first?'0':'10px'} 0 3px">${text}</div>`;
    const block = (text, cls) =>
      `<div class="quote ${cls||''}" style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:280px;overflow:auto">${esc(text)}</div>`;
    const meta = rec.metadata && typeof rec.metadata === 'object' ? rec.metadata : {};
    const metaKeys = Object.keys(meta).filter(k => meta[k] != null && meta[k] !== '').sort();
    const parts = [];
    if(rec.input && !skip.input) parts.push(['INPUT', block(rec.input)]);
    if(rec.response && !skip.response) parts.push(['RESPONSE', block(rec.response)]);
    if(rec.error && !skip.error) parts.push(['ERROR', block(rec.error, 'st-red')]);
    if(metaKeys.length) parts.push(['METADATA', kv(metaKeys.map(k =>
      [k, esc(typeof meta[k] === 'object' ? JSON.stringify(meta[k]) : String(meta[k])), 'mono']))]);
    return parts.map((p, i) => label(p[0], i === 0) + p[1]).join('');
  }

  /* ---------------- the LLMs Used panel ----------------
     Sits in the KPI row beside Total Runs. The server folds `models` in the
     same pass as total_runs, so the slices always add up to the Total Runs
     card: the donut is that number, split by model. The five biggest named
     models get a slice each, the rest share "Other", and runs that name no
     model at all -- not on the run, not on the agent -- are a grey "Not
     recorded" slice rather than being dropped or spread across the others. */
  const MODEL_COLORS = ['purple','blue','green','amber','cyan'];
  const MODEL_SHOWN = 5;
  /* 672px is the narrowest the four cards stay on one row (.kpi-row's 158px
     minimum each, three 12px gaps). With the panel's 340px that puts the panel
     beside the cards from a 1366px-wide window up; narrower than that it takes
     the full width below them, and its legend spreads into more columns. */
  const KPI_BASIS = 'flex:2 1 672px;min-width:0;margin-bottom:0';
  const LLM_PANEL_BASIS = 'flex:1 1 340px;min-width:0';

  /** The four cards and the panel, side by side; they wrap on a narrow screen. */
  function kpiArea(cardsHtml, panelHtml){
    return `<div style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:16px">
      <div class="kpi-row" style="${KPI_BASIS}">${cardsHtml}</div>${panelHtml}</div>`;
  }

  function kpiAreaSkeleton(labels){
    const card = (label, style) => `<div class="kpi-card card-loading" ${style ? `style="${style}"` : ''}>
      <div class="kpi-top"><div class="kpi-label">${esc(label)}</div></div><div class="kpi-value">&nbsp;</div></div>`;
    return kpiArea(labels.map(l => card(l)).join(''), card('LLMs Used', LLM_PANEL_BASIS));
  }

  /** Top five named models, then Other, then Model not recorded; empty slices dropped. */
  function modelSlices(models){
    const named = models.filter(m => m.model != null).sort((a, b) => b.runs - a.runs);
    const unnamed = models.filter(m => m.model == null);
    const sum = (rows, key) => rows.reduce((total, row) => total + (row[key] || 0), 0);
    const slices = named.slice(0, MODEL_SHOWN).map((m, i) => ({
      label: m.model, runs: m.runs, tokens: m.tokens, cost: m.cost,
      fromAgent: m.runs_model_from_agent || 0, color: MODEL_COLORS[i] }));
    const rest = named.slice(MODEL_SHOWN);
    if(rest.length) slices.push({
      label: 'Other', runs: sum(rest, 'runs'), tokens: sum(rest, 'tokens'), cost: sum(rest, 'cost'),
      fromAgent: sum(rest, 'runs_model_from_agent'), color: 'pink', members: rest.map(m => m.model) });
    if(unnamed.length) slices.push({
      label: 'Model not recorded', runs: sum(unnamed, 'runs'), tokens: sum(unnamed, 'tokens'),
      cost: sum(unnamed, 'cost'), color: 'gray', unrecorded: true });
    return slices.filter(s => s.runs > 0);
  }

  /** The summary's time_range, as the short suffix the governance screens put on a count. */
  const WINDOW_SHORT = { 'Last hour':'1h', 'Last 6 hours':'6h', 'Last 24 hours':'24h' };

  /* The run filters a violation record cannot answer, as the summary names
     them in violations_scope.filters_not_applied: [filter label, what it is of a run]. */
  const FILTER_NAMES = { tenant:['Tenant','tenant'], source:['Source','source'], status:['Status','status'],
    risk:['Risk','risk'], policy:['Policy','policy verdict'], q:['search','text'] };
  function listOf(items){
    return items.length < 2 ? items.join('') : items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1];
  }

  function sharePct(part, whole){
    if(!whole) return '—';
    const p = part / whole * 100;
    return (p >= 10 || p === 0 ? p.toFixed(0) : p < 0.1 ? '<0.1' : p.toFixed(1)) + '%';
  }

  function llmPanel(s){
    const slices = modelSlices(s.models || []);
    // The slices are folded with total_runs and add up to it; summing them
    // rather than trusting the field keeps the percentages honest either way.
    const total = slices.reduce((t, x) => t + x.runs, 0);
    const scan = s.scan || {};
    const capped = scan.truncated
      // runs_scanned is what was read, before the Tenant and Source filters:
      // the donut can hold fewer, so this names the read, not the donut.
      ? `Read from the most recent ${fmtFull(scan.runs_scanned)} runs — the window was capped, so these are a floor.` : null;
    const share = (x) => `${fmtFull(x.runs)} run${x.runs === 1 ? '' : 's'} (${sharePct(x.runs, total)})`;
    const hint = (x) => [
      `${x.label}: ${share(x)}`,
      `${fmtFull(x.tokens)} tokens`, fmtMoney(x.cost, 2),
      x.members ? `models: ${x.members.join(', ')}` : null,
      // The Model column's rule: a run that names no model is counted under the
      // one its agent is registered with. Say how many, rather than let a
      // registration read as something every one of these runs reported.
      x.fromAgent ? `${fmtFull(x.fromAgent)} of these named no model and are counted under their agent's registered model` : null,
      x.unrecorded ? 'neither the run nor its agent names a model' : null,
    ].filter(Boolean).join(' · ');
    /* As many 150px columns as the panel has room for: one beside the cards
       on a laptop screen, two on a wide one, more when the panel sits on its
       own row. Filled row by row, so the ranking reads left to right. */
    const legend = slices.length
      ? `<div class="legend" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:3px 14px;margin-top:8px">${
          slices.map(x => `<div class="legend-item" style="font-size:11.5px;gap:6px;min-width:0" title="${esc(hint(x))}">
            <span class="sw" style="background:${U.cc(x.color)}"></span>
            <span class="lg-label" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0${x.unrecorded ? ';color:var(--text-dim)' : ''}">${esc(x.label)}</span>
            <span class="lg-val">${esc(fmtNum(x.runs))}</span>
            <span class="lg-pct" style="width:auto;min-width:30px">${esc(sharePct(x.runs, total))}</span></div>`).join('')}</div>`
      : `<div class="faint small" style="margin-top:10px">No runs in this window.</div>`;
    // The accessible name carries the figures too: a screen reader otherwise
    // hears "link" and none of what the donut and its legend show.
    const spoken = `LLMs used in this window: ${slices.length
      ? slices.map(x => `${x.label}, ${share(x)}`).join('; ') : 'no runs'}.${
      capped ? ' ' + capped : ''} Open LLM Usage.`;
    return `<div class="kpi-card" data-nav="llm-usage" data-llm-panel role="link" tabindex="0"
        aria-label="${esc(spoken)}" title="Open LLM Usage"
        style="${LLM_PANEL_BASIS};display:flex;align-items:center;gap:14px;cursor:pointer">
        ${donut({ segments: slices.map(x => ({ value: x.runs, color: x.color, label: x.label })),
                  size: 72, thickness: 10,
                  centerVal: `<span style="font-size:15px">${esc(fmtNum(total))}</span>`,
                  centerLabel: total === 1 ? 'run' : 'runs' })}
        <div style="flex:1;min-width:0">
          <div class="kpi-top" style="align-items:center">
            <div class="kpi-label">LLMs Used</div>
            <span class="link" style="font-size:11px">LLM Usage ${ICONS.arrowRight}</span></div>
          ${legend}
          ${capped ? `<div class="kpi-sub" style="margin-top:6px">${esc(capped)}</div>` : ''}
        </div></div>`;
  }

  /* ================= LIVE RUNS ================= */
  SCREENS['live-runs'] = {
    title:'Live Runs',
    render(main){
      let liveOn = true, runStream = null, streamKey = '', currentRunId = null;
      // Set by cleanup. A request that was in flight when the screen was left
      // still resolves into these closures; every path that would start new
      // work (a summary, a stream, an inspector read) checks this first.
      let disposed = false;
      const SUMMARY_LABELS = ['Total Runs','Success Rate','Avg Latency','Policy Violations'];

      main.innerHTML = `
        ${pageHead({ title:'Live Runs', sub:'Real-time observability of agent executions across every connected platform.',
          actions:`${searchBox('lrSearch','Search runs…')}
            <span class="live-pill" id="livePill"><span class="dot pulse" style="background:currentColor"></span>LIVE</span>
            <button class="btn" id="lrExport">${ICONS.download}Export</button>
            <button class="btn orange" id="btnRun">${ICONS.play}Run</button>` })}
        <div class="chip-row" id="lrChips"></div>
        <div id="lrKpis">${kpiAreaSkeleton(SUMMARY_LABELS)}</div>
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
            `<span class="conn-chip" data-nav="connections"><span class="logo">${connLogo(c)}</span>${esc(c.name)}<span class="stat"><span class="dot"></span>${esc(c.status)}</span></span>`
          ).join('');
        })
        .catch(()=>{ const el = document.getElementById('lrChips'); if(el) el.innerHTML = ''; });

      /* The mark the server named, then the one its kind implies. This used to
         read `c.platform`, which ConnectionRead does not return, so every chip
         on the row fell through to the generic mark whatever it was connected
         to. `logo_key` is what the Connection Center itself draws from. */
      function connLogo(c){
        return LOGOS[c.logo_key || KIND_LOGO[c.kind]] || LOGOS.custom;
      }

      // ---- KPI cards + the four sparkline KPIs -------------------------
      /* One summary is two window scans, each fanning out to every reporting
         agent — the most expensive read on the screen. It used to be asked for
         after every table load (so every sort, page and search), once more at
         mount for the tenant list, and once per streamed run, with nothing in
         flight ever superseded: a busy workspace kept several running at once,
         each slowing the others until the cards timed out. It depends on three
         filters and on time, so those are the only two reasons it is read:

           - the filters it answers changed        -> now (loadSummary)
           - runs arrived, or the tab came back    -> at most once per
             SUMMARY_REFRESH_MS, trailing, never while one is in flight
             (summarySoon)

         The server memoises a summary for the same 20 s, so asking sooner
         would only be handed the same numbers. */
      const SUMMARY_REFRESH_MS = 20000;
      let summaryKey = null;      // the filters the cards on screen (or on their way) answer
      let summaryAt = 0;          // when the last answer, good or bad, arrived
      let summarySeq = 0, summaryInflight = false, summaryDirty = false, summaryTimer = null;

      function summaryParams(){
        const p = table ? table.params() : {};
        return { time_range: p.time_range, tenant: p.tenant, source: p.source };
      }

      function loadSummary(){
        const host = document.getElementById('lrKpis');
        const mini = document.getElementById('lrMini');
        if(disposed || !host) return;
        const params = summaryParams();
        const key = JSON.stringify(params);
        // The same question is already on its way; its answer will do.
        if(summaryInflight && key === summaryKey) return;
        clearTimeout(summaryTimer); summaryTimer = null;
        // Numbers for other filters must not sit under the new ones while the
        // request runs. A refresh of the same filters keeps its cards.
        if(key !== summaryKey){
          host.innerHTML = kpiAreaSkeleton(SUMMARY_LABELS);
          if(mini) mini.innerHTML = '';
        }
        summaryKey = key;
        summaryInflight = true; summaryDirty = false;
        // A change of filters does not wait for the request it supersedes; the
        // ticket makes sure that one's late answer is never painted.
        const ticket = ++summarySeq;
        API.runs.summary(params)
          .then(s => {
            if(disposed || ticket !== summarySeq || !document.getElementById('lrKpis')) return;
            summaryInflight = false; summaryAt = Date.now();
            paintSummary(s);
            fillTenants(s.tenants);
            if(summaryDirty) summarySoon();
          })
          .catch(err => {
            if(disposed || ticket !== summarySeq || !document.getElementById('lrKpis')) return;
            summaryInflight = false; summaryAt = Date.now();
            host.innerHTML = '';
            host.appendChild(screenError(err, loadSummary, 'the run summary'));
            if(mini) mini.innerHTML = '';
            // Runs that arrived meanwhile still earn a retry — a throttled one.
            if(summaryDirty) summarySoon();
          });
      }

      /** Read the cards again only if the filters they answer have changed. */
      function summaryIfChanged(){
        if(!disposed && JSON.stringify(summaryParams()) !== summaryKey) loadSummary();
      }

      /** Refresh the cards for the filters already on screen: trailing, at most
       *  one per SUMMARY_REFRESH_MS, and never on top of one in flight. */
      function summarySoon(){
        if(disposed) return;
        if(summaryInflight){ summaryDirty = true; return; }
        if(summaryTimer) return;
        const wait = Math.max(0, summaryAt + SUMMARY_REFRESH_MS - Date.now());
        summaryTimer = setTimeout(()=>{ summaryTimer = null; loadSummary(); }, wait);
      }

      function paintSummary(s){
        const host = document.getElementById('lrKpis');
        const mini = document.getElementById('lrMini');
        const note = document.getElementById('lrScanNote');
        const scan = s.scan || {};
        const vscope = s.violations_scope || {};
        // When either window hit the scan cap the server withholds every delta
        // folded from the scan (they arrive null); say why the trend is missing.
        const withheld = s.comparable === false ? 'trend withheld — a window was capped' : null;
        /* Policy Violations is NOT folded from the scan. It counts the violation
           records the Policy Center counts — every one in the window for the
           agents in view — so it is complete under any cap and its trend is
           never withheld. It used to count runs whose verdict was not Allowed,
           which disagreed with the Policy Center; the Policy column in the
           table still shows each run's own verdict.
           The Policy Center counts the same records over 30 days and says so
           in its labels ("Policy Violations (30d)"); these name their own
           window the same way, so two different windows are never read as two
           different answers to one question. */
        const span = WINDOW_SHORT[s.time_range] || null;
        const windowed = (label) => span ? `${label} (${span})` : label;
        const cards = [
          { label:'Total Runs', value:fmtFull(s.total_runs), icon:'activity', color:'purple',
            delta: s.total_runs_delta_percent == null ? null : Math.abs(s.total_runs_delta_percent).toFixed(1)+'%',
            dir: deltaDir(s.total_runs_delta_percent), good: s.total_runs_delta_percent >= 0, vs:'vs previous period', sub: withheld },
          { label:'Success Rate', value: s.success_rate == null ? '—' : pct(s.success_rate), icon:'target', color:'green',
            delta: s.success_rate_delta_points == null ? null : Math.abs(s.success_rate_delta_points).toFixed(1)+' pp',
            dir: deltaDir(s.success_rate_delta_points), good: s.success_rate_delta_points >= 0, vs:'vs previous period', sub: withheld },
          { label:'Avg Latency', value: secs(s.avg_latency_seconds), icon:'clock', color:'amber',
            delta: s.avg_latency_delta_seconds == null ? null : Math.abs(s.avg_latency_delta_seconds).toFixed(2)+'s',
            dir: deltaDir(s.avg_latency_delta_seconds), good: s.avg_latency_delta_seconds <= 0, vs:'vs previous period', sub: withheld },
          { label:windowed('Policy Violations'), value:fmtFull(s.policy_violations), icon:'shield', color:'red',
            delta: s.policy_violations_delta_percent == null ? null : Math.abs(s.policy_violations_delta_percent).toFixed(1)+'%',
            dir: deltaDir(s.policy_violations_delta_percent), good: s.policy_violations_delta_percent <= 0, vs:'vs previous period',
            sub: s.policy_violations_previous === 0 && s.policy_violations > 0
              ? 'none in the previous period' : 'recorded violations, as in Policy Center' },
        ];
        host.innerHTML = kpiArea(cards.map(kpiCard).join(''), llmPanel(s));
        const panel = host.querySelector('[data-llm-panel]');
        // The click is the app's own [data-nav] handler; the keyboard needs this.
        if(panel) panel.addEventListener('keydown', e => {
          if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); APP.go('llm-usage'); }
        });

        /* A capped scan holds each agent's NEWEST runs, so the window is read
           whole only from `covered_from` on. A bucket before that is not a
           measurement — drawing it made a busy morning look like a flat line
           of zeros — so it is not drawn. sparkline() needs two points.
           Human Escalations is the exception: it is counted from the violation
           records, not the scan, so every one of its buckets was measured.
           Agent Hand-offs is the agent's own "I passed this to a person" flag
           on the run, which Human Escalations used to count. */
        const from = scan.truncated && scan.covered_from ? ts(scan.covered_from) : null;
        const counted = s.human_escalations;
        if(mini) mini.innerHTML = [
          [s.tokens_used, 'green'], [s.estimated_cost, 'orange'], [s.fallback_rate, 'orange'],
          [s.human_escalations, 'purple'], [s.agent_handoffs, 'amber'],
        ]
          .filter(([k]) => Boolean(k))
          .map(([k, color]) => {
            const points = (k.series || [])
              .filter(p => k === counted || from == null || ts(p.at) >= from).map(p => p.value);
            return miniKpi({
              label: k === counted ? windowed(k.label) : k.label,
              value: formatSpark(k),
              // A figure that was not measured (a rate over no runs) prints a
              // dash; a flat line under it would read as a measured zero.
              spark: k.value != null && points.length >= 2 ? points : null,
              color,
            });
          }).join('');

        const notes = [];
        if(scan.truncated) notes.push(`Showing the most recent ${fmtFull(scan.runs_scanned)} runs across ${fmtFull(scan.agents_scanned)} of ${fmtFull(scan.agents_total)} agents — the window was capped, so the run figures above are a floor, not a complete count.${
          from == null ? '' : ` They are complete from ${esc(U.fmtDateTime(from))} onwards; before that the window was only partly read, and the trend lines start there.`
        } Policy Violations and Human Escalations are counted from the violation records, not the runs read, and are complete.`);
        const unapplied = vscope.filters_not_applied || [];
        if(unapplied.length) notes.push(`Policy Violations and Human Escalations count every violation recorded against the agents in view. The ${
          esc(listOf(unapplied.map(f => (FILTER_NAMES[f] || [f])[0])))} filter${unapplied.length === 1 ? ' does' : 's do'} not narrow them — a violation record names its agent, not the run’s ${
          esc(listOf(unapplied.map(f => (FILTER_NAMES[f] || [f, f])[1])))}.`);
        if(note) note.innerHTML = notes.map(n => `<div class="scan-note">${ICONS.info} ${n}</div>`).join('');
      }

      /* The Tenant filter's options are workspace-specific and ride on the
         summary. They used to cost a second, identical summary at mount. A
         summary narrowed to one tenant names only that tenant, so names are
         only ever added, never taken away. */
      function fillTenants(tenants){
        const select = table && table.filterEl && table.filterEl.querySelector('[data-fi="0"]');
        if(!select) return;
        const have = new Set(Array.from(select.options).map(o => o.value || o.textContent));
        (tenants || []).forEach(t => {
          if(!t || have.has(t)) return;
          const opt = document.createElement('option');
          opt.textContent = t; select.appendChild(opt);
          have.add(t);
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

      /* `scan.truncated` on a page means the list was cut at the scan cap and
         `total` is a floor. The footer prints cfg.totalOverride in place of the
         count when one is set, so the floor is worded as one ("of at least
         2,000 runs") instead of passing for the size of the window. It is set
         before the table paints the page, and only by the newest request. */
      let listSeq = 0;
      function listRuns(params){
        const my = ++listSeq;
        return API.runs.list(params).then(page => {
          if(my === listSeq){
            tableCfg.totalOverride = page && page.scan && page.scan.truncated && page.total != null
              ? 'at least ' + fmtFull(page.total) : null;
          }
          return page;
        });
      }

      const tableCfg = {
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
        source: listRuns,
        exportSource: (params) => API.runs.export(params),
        // Every sort, page and search lands here. The cards depend on none of
        // those, so they are re-read only when their own filters changed. A
        // load that resolves after the screen was left does nothing at all.
        onLoad: () => {
          if(disposed) return;
          summaryIfChanged();
          syncStream();
        },
        autoSelectFirst: true,
        onSelect: showRun,
        rowActions: r=>[
          {label:'View Full Trace', icon:'activity', onClick:()=>openTrace(r.id)},
          {label:'Open in Replay Studio', icon:'replay', onClick:()=>{ APP.replayRun = r.id; APP.go('replay'); }},
          ...(r.agent_id ? [{label:'View Agent', icon:'bot', onClick:()=>APP.go('agent/'+r.agent_id)}] : []),
          ...(Store.session.can('member') ? [
            {sep:true},
            {label:'Flag for Review', icon:'flag', onClick:()=>flagRun(r)},
          ] : []),
        ],
      };
      const table = dataTable(tableCfg);

      const wrap = document.getElementById('lrTableWrap');
      wrap.appendChild(table.filterEl);
      wrap.appendChild(table.el);
      // table.search() waits out the typing (300 ms) before it asks the server.
      document.getElementById('lrSearch').addEventListener('input', e=>table.search(e.target.value));

      // The cards do not wait for the run list: if the list fails they still
      // load, and the Tenant filter is filled from this same first summary.
      // A filter change is heard here as well as in onLoad, so the cards stop
      // showing the old filters' numbers while the new list is still loading.
      // (The table's own listeners were attached first and have already put
      // the new value in table.params() by the time these run.)
      loadSummary();
      table.filterEl.addEventListener('change', summaryIfChanged);
      table.filterEl.querySelector('.clear-filters').addEventListener('click', summaryIfChanged);

      // The filtered set as CSV, from the server — the table always carried the
      // export, but no control on the screen ever called it.
      const exportBtn = document.getElementById('lrExport');
      exportBtn.addEventListener('click', async () => {
        if(exportBtn.disabled) return;
        exportBtn.disabled = true;
        try { await table.export(); } finally { exportBtn.disabled = false; }
      });

      async function flagRun(r){
        if(!Store.session.can('member')){
          toast('error','Not permitted','Flagging a run requires the member role.');
          return;
        }
        try {
          const result = await API.runs.flag(r.id, { reason: 'Flagged from Live Runs' });
          toast('warn','Flagged for review', `${String(result.run_id).slice(0,12)}… routed to the review queue.`);
          if(!disposed) table.refresh();
        } catch (err) {
          toast('error','Could not flag run', err.message);
        }
      }

      // ---- inspector ----------------------------------------------------
      function showRun(row){
        const insp = document.getElementById('lrInspector');
        // A first load that lands after the screen was left still auto-selects
        // its first row; that must not cost a run read, nor paint into the
        // inspector of a Live Runs screen opened since.
        if(disposed || !insp || !row) return;
        currentRunId = row.id;
        document.getElementById('lrLayout').classList.remove('collapsed');
        insp.innerHTML = `<div class="insp-head"><div>
            <div class="insp-title">Run Details</div>
            <div class="insp-sub mono">${esc(row.id)}</div></div>
            <button class="icon-btn insp-close" id="lrInspClose">${ICONS.x}</button></div>
          <div class="card-loading" style="height:220px;margin:12px"></div>`;
        insp.querySelector('#lrInspClose').addEventListener('click', ()=>document.getElementById('lrLayout').classList.add('collapsed'));

        API.runs.get(row.id)
          .then(r => { if(!disposed && currentRunId === row.id) paintRun(insp, r); })
          .catch(err => {
            if(disposed || currentRunId !== row.id) return;
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
            // The run's own flag -- what the Agent Hand-offs figure counts, not
            // the Human Escalations one, which counts violation records.
            ['Agent Hand-off', err.escalated ? '<span class="st-amber">Yes</span>' : 'No'],
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
                // A failed run usually has no response at all — what it has is
                // an error, and a blank dialog under "Full Response" hid it.
                body.innerHTML = `<div class="quote" style="font-size:12.5px"><b>${esc(r.agent||'—')}</b> · ${esc(r.model||'—')} · ${fmtTime(ts(r.occurred_at))} · ${fmtFull(r.output_tokens)} tokens · ${fmtFull(r.character_count)} chars</div>
                  ${r.response ? `<p style="white-space:pre-wrap">${esc(r.response)}</p>`
                    : `<p class="faint">${r.error ? 'This run recorded no response — it ended with the error below.' : 'This run recorded no response.'}</p>`}
                  ${r.error ? `<div class="small muted" style="font-weight:700;margin:10px 0 3px">ERROR</div>
                    <div class="quote st-red" style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(r.error)}</div>` : ''}`;
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
                  ${traceBody(t)}`;
              })
              .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, null, 'the execution trace')); });
          },
        });
      }

      /* The span tree when there is one. A run reported as a single decorated
         call has none, and "No spans were recorded" was then the whole dialog
         although the run carries its own input, output, error and metadata —
         so that is what a span-less run shows, labelled as the run's own
         record rather than dressed up as a span. */
      function traceBody(t){
        if(t.spans && t.spans.length){
          return `${t.error ? `<div class="scan-note st-red" style="margin:0 0 12px">${ICONS.alert}<span style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(t.error)}</span></div>` : ''}
            <div class="pipe">${renderSpans(t.spans, 0)}</div>`;
        }
        const record = runRecord(t);
        if(!record){
          return '<div class="empty-state">'+ICONS.search+'<div class="es-title">No spans were recorded for this run</div><div>The agent reported the run but not its internal steps, and nothing was recorded on the run itself.</div></div>';
        }
        return `<div class="scan-note" style="margin:0 0 12px">${ICONS.info} No spans were recorded — the agent reported this run as a single call. What follows is what the run itself recorded.</div>
          ${record}`;
      }

      function renderSpans(spans, depth){
        return spans.map(s => {
          const state = s.status === 'fail' || s.error ? 'fail' : 'done';
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

      function streamParams(){
        const p = table.params();
        return { tenant: p.tenant, source: p.source, status: p.status, risk: p.risk, policy: p.policy, q: p.q };
      }

      function openStream(){
        // Never after cleanup: a run list that resolved late used to come
        // through onLoad -> syncStream and open an EventSource whose only
        // handle lived in this dead closure. Nothing could close it, so it
        // polled the telemetry store and held a stream slot for the life of
        // the tab.
        if(disposed || runStream) return;
        const p = streamParams();
        streamKey = JSON.stringify(p);
        runStream = API.runs.stream({
          params: p,
          events: {
            open: () => setPill('live'),
            run: (frame) => {
              if(disposed || !frame || !frame.run || !liveOn) return;
              table.prependRow(frame.run);
              // A burst of runs is one refresh of the cards, not one each.
              summarySoon();
            },
          },
          onError: () => { if(!disposed) setPill('reconnecting'); },
          /* api.js parks the stream while the tab sits in the background and
             reconnects when it is looked at again; the stream only carries
             runs that start from then on, so whatever was reported while it
             was parked is read back here. */
          onResume: () => {
            if(disposed || !liveOn) return;
            table.refresh();
            summarySoon();
          },
        });
      }

      function closeStream(){
        if(runStream){ runStream.close(); runStream = null; }
      }

      function syncStream(){
        // The stream answers the query it was opened with; once the filters or
        // search change it would keep pushing rows the table no longer shows.
        if(disposed || !liveOn) return;
        if(runStream && JSON.stringify(streamParams()) === streamKey) return;
        closeStream();
        openStream();
      }

      pill.addEventListener('click', ()=>{
        liveOn = !liveOn;
        if(liveOn){ openStream(); setPill('live'); }
        else { closeStream(); setPill('paused'); }
        toast('info', liveOn?'Live stream resumed':'Live stream paused',
          liveOn?'New runs appear as they are reported.':'The connection is closed until you resume.');
      });
      openStream();
      this.cleanup = () => {
        disposed = true;
        clearTimeout(summaryTimer); summaryTimer = null;
        closeStream();
      };

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
                const d = started.data || {};
                toast('success','Run started', `${esc(d.agent_name || 'Agent')} · ${String(d.run_id || started.entity_id || '').slice(0,12)}…`);
                if(!disposed) table.refresh();
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
      let cursor = null;        // next_cursor of the rail's last page; null = exhausted
      let railAgent = null;     // agent whose history the rail is showing

      main.innerHTML = `
        ${pageHead({title:'Replay Studio', sub:'Pick an agent, browse every run it has ever recorded, and step through any of them.',
          actions:`<button class="btn primary" id="rpPlay" disabled>${ICONS.play}Play Replay</button>`})}
        <div class="rp-layout">
          <div class="card" style="padding-bottom:0">
            <div class="card-head" style="margin-bottom:10px"><div class="card-title">Run Browser</div>
              <div class="faint small" id="rpCount"></div></div>
            <select class="filter-select" id="rpAgent" style="width:100%;height:34px;margin-bottom:10px">
              <option>Loading agents…</option></select>
            <div class="rp-rail-list" id="rpRail"><div class="card-loading" style="height:120px;margin:0 14px 14px"></div></div>
          </div>
          <div id="rpBody"><div class="empty-state">${ICONS.replay}
            <div class="es-title">Pick a run to replay</div>
            <div>Choose an agent on the left, then any run from its history.</div></div></div>
        </div>`;

      const body = document.getElementById('rpBody');
      const rail = document.getElementById('rpRail');
      const agentPick = document.getElementById('rpAgent');
      const countEl = document.getElementById('rpCount');
      const playBtn = document.getElementById('rpPlay');

      const railRuns = [];      // every run the rail has fetched so far, newest first

      function runRow(r){
        const facts = [
          r.model,
          r.tokens ? fmtFull(r.tokens)+' tok' : null,
          r.duration_seconds == null ? null : secs(r.duration_seconds),
        ].filter(Boolean).join(' · ');
        return `<button class="rp-run ${r.id===APP.replayRun?'active':''}" data-run="${esc(r.id)}"
            title="${esc(r.id)}">
          <div class="rp-run-top">${statusText(r.status||'—')}
            <span class="faint small" title="${esc(String(r.occurred_at||''))}">${relTime(ts(r.occurred_at))}</span></div>
          <div class="rp-run-preview">${r.input_preview ? esc(r.input_preview) : '<span class="faint">no input recorded</span>'}</div>
          <div class="rp-run-facts mono">${esc(String(r.id).slice(0,14))}…${facts?` · ${esc(facts)}`:''}</div>
        </button>`;
      }

      function paintRail(){
        if(!railRuns.length){
          rail.innerHTML = `<div class="empty-state" style="padding:26px 14px">${ICONS.replay}
            <div class="es-title" style="font-size:13px">No runs recorded</div>
            <div class="small">This agent has never reported a run.</div></div>`;
          countEl.textContent = '';
          return;
        }
        rail.innerHTML = railRuns.map(runRow).join('') +
          (cursor ? `<div class="rp-more"><button class="btn" id="rpMore" style="width:100%">Load older runs</button></div>`
                  : `<div class="rp-more faint small" style="text-align:center">Full history — ${railRuns.length} runs</div>`);
        countEl.textContent = `${railRuns.length} run${railRuns.length===1?'':'s'}${cursor?' so far':''}`;
        rail.querySelectorAll('.rp-run').forEach(el =>
          el.addEventListener('click', ()=>load(el.dataset.run)));
        const more = document.getElementById('rpMore');
        if(more) more.addEventListener('click', ()=>fetchPage(railAgent));
      }

      function markActive(runId){
        rail.querySelectorAll('.rp-run').forEach(el =>
          el.classList.toggle('active', el.dataset.run === runId));
      }

      function fetchPage(agentId, andThen){
        const first = !cursor || railAgent !== agentId;
        if(first){ railRuns.length = 0; cursor = null; railAgent = agentId;
          rail.innerHTML = '<div class="card-loading" style="height:120px;margin:0 14px 14px"></div>'; }
        const more = document.getElementById('rpMore');
        if(more){ more.disabled = true; more.textContent = 'Loading…'; }
        API.runs.history({ agent_id: agentId, cursor: cursor || undefined, limit: 50 })
          .then(page => {
            if(railAgent !== agentId) return; // agent switched while loading
            railRuns.push(...(page.items||[]));
            cursor = page.next_cursor || null;
            paintRail();
            if(andThen) andThen();
          })
          .catch(err => {
            rail.innerHTML = '';
            const box = screenError(err, ()=>fetchPage(agentId, andThen), 'this agent’s history');
            box.style.margin = '0 14px 14px';
            rail.appendChild(box);
          });
      }

      // Agents come from the registry — every agent, whatever its status,
      // because history outlives activation. The rail then reads that agent's
      // complete run history from the telemetry store, page by page.
      // A run can be named internally (Live Runs hand-off) or from outside
      // via #/replay?run=<id> — the chat frontend links each answer that way.
      const requested = (APP.query && APP.query.run) || APP.replayRun;
      Promise.all([
        API.agents.list({ page_size: 200, sort: 'name' }),
        requested ? API.runs.get(requested).catch(() => null) : Promise.resolve(null),
      ])
        .then(([agents, named]) => {
          const items = agents.items || [];
          if(!items.length){
            agentPick.innerHTML = '<option>No agents registered</option>';
            rail.innerHTML = `<div class="empty-state" style="padding:26px 14px">${ICONS.replay}
              <div class="es-title" style="font-size:13px">Nothing to replay yet</div>
              <div class="small">Register an agent and report a run first.</div></div>`;
            return;
          }
          agentPick.innerHTML = items.map(a =>
            `<option value="${esc(a.id)}">${esc(a.name)} — ${esc(a.status||'')}</option>`).join('');
          agentPick.addEventListener('change', ()=>{ fetchPage(agentPick.value); });

          /* The run asked for wins. Arriving here from Live Runs names one
           * specific run; replaying a different one silently would be the
           * worst possible failure for a debugging tool. So a named run picks
           * its own agent and loads directly, and if it cannot be fetched
           * that is said plainly rather than quietly swapped. */
          if(requested && named){
            // Its own agent's history, and only that one: the rail never walks
            // the first agent in the picker on the way to a named run. An agent
            // the picker does not list (beyond its 200) still gets its run
            // loaded; the rail then shows the picker's agent, as it says.
            if(named.agent_id && items.some(a=>a.id===named.agent_id)) agentPick.value = named.agent_id;
            fetchPage(agentPick.value, ()=>markActive(requested));
            load(requested);
            return;
          }
          if(requested && !named){
            body.innerHTML = `<div class="empty-state">${ICONS.alert}
              <div class="es-title">That run could not be loaded</div>
              <div>Run <span class="mono">${esc(String(requested).slice(0,14))}…</span> is not available —
              it may have passed its retention window. Pick another run from the browser.</div></div>`;
            APP.replayRun = null;
          }
          fetchPage(agentPick.value);
        })
        .catch(err => {
          agentPick.innerHTML = '<option>Unavailable</option>';
          rail.innerHTML = '';
          body.innerHTML = '';
          body.appendChild(screenError(err, ()=>SCREENS['replay'].render(main), 'the agent list'));
        });

      function load(runId){
        APP.replayRun = runId;
        markActive(runId);
        playBtn.disabled = true;
        body.innerHTML = '<div class="card-loading" style="height:180px"></div>';
        API.runs.replay(runId)
          .then(session => paint(session))
          .catch(err => { body.innerHTML = ''; body.appendChild(screenError(err, ()=>load(runId), 'this replay')); });
      }

      function paint(session){
        const r = session.run || {};
        const steps = session.steps || [];
        // The run's own record sits under the timeline. A step carries a
        // preview, so whatever a step already shows word for word is not
        // printed twice; a longer input or output appears here in full.
        const shown = (field, text) => Boolean(text) && steps.some(s => s[field] === text);
        const record = runRecord(session, {
          input: shown('prompt', session.input),
          response: shown('response', session.response),
          error: shown('error', session.error),
        });
        body.innerHTML = `
          <div class="kpi-row" style="grid-template-columns:repeat(auto-fit,minmax(170px,1fr))">
            ${kpiCard({label:'Run', value:`<span style="font-size:15px" class="mono">${esc(String(r.id||'').slice(0,14))}…</span>`, sub:r.agent||'—', icon:'activity', color:'purple'})}
            ${kpiCard({label:'Status', value:statusText(r.status||'—'), sub:r.model||'—', icon:'target', color:'green'})}
            ${kpiCard({label:'Duration', value:secs(session.total_duration_ms == null ? r.duration_seconds : session.total_duration_ms/1000), sub:`${session.step_count} steps`, icon:'clock', color:'amber'})}
            ${kpiCard({label:'Fidelity', value:session.fidelity == null ? '—' : pct(session.fidelity,0), sub:`${session.captured_steps} of ${session.step_count} steps with full payloads`, icon:'layers', color:'blue'})}
          </div>
          ${session.steps_from_trace ? `<div class="scan-note">${ICONS.info} This run recorded no spans — the agent reported it as a single call. The steps below were built from the run’s own input and output, not from recorded spans.</div>` : ''}
          ${session.replayable ? '' : `<div class="scan-note">${ICONS.info} This run was reported without step payloads, so the timeline shows what was recorded and no more.</div>`}
          <div class="card" style="margin-top:14px">
            <div class="card-head"><div class="card-title">Execution Timeline</div>
              <div class="faint small">${session.step_count} steps</div></div>
            <div class="pipe" id="rpSteps">${steps.map((s,i)=>stepHtml(s,i)).join('')}</div>
          </div>
          ${record ? `<div class="card" style="margin-top:14px">
            <div class="card-head"><div class="card-title">Recorded on the Run</div>
              <div class="faint small">the run’s own input, output, error and metadata</div></div>
            ${record}
          </div>` : ''}`;
        playBtn.disabled = !steps.length;
        playBtn.onclick = ()=>play(session);
      }

      function stepHtml(s, i){
        const state = s.status === 'fail' || s.error ? 'fail' : 'done';
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

      function loadAll(){ loadOverview(); loadCharts(); }

      /* Every change of window or agent starts both loaders again, and a wide
         window answers slower than a narrow one. Whatever resolved last used to
         be painted, so switching 30d -> 24h could leave the 30-day cards and
         charts under a picker reading "Last 24 hours", with nothing on screen
         to give it away. Each loader takes a ticket, the way dataTable.load()
         does, and only the newest request of each kind may paint. The counter
         lives in the loader, not in loadAll(), because "Try again" calls the
         loader directly. */
      let overviewSeq = 0, chartSeq = 0;

      const KPI_LABELS = ['Total Runs','Success Rate','p50 Latency','p90 Latency','Tokens','Cost'];

      /* The six cards and the models table are two views of one measurement —
         the window's per-agent rollup — and /metrics/overview answers both from
         a single pass over the store. They used to be /metrics/summary plus a
         server-mode table on /metrics/models, which measured the same rollup
         again for every sort click and page. The breakdown is a handful of
         rows, so it arrives whole and the table sorts and pages it here. */
      function loadOverview(){
        const host = document.getElementById('mtKpis');
        const models = document.getElementById('mtModels');
        if(!host) return;
        const my = ++overviewSeq;
        host.innerHTML = kpiSkeleton(KPI_LABELS);
        if(models) models.innerHTML = '<div class="card"><div class="card-loading" style="height:160px"></div></div>';
        API.metrics.overview(q())
          .then(o => {
            if(my !== overviewSeq || !host.isConnected) return;
            const s = o.summary || {};
            host.innerHTML = kpiRow(
              [s.total_runs, s.success_rate, s.latency_p50, s.latency_p90, s.tokens, s.cost]
                .filter(Boolean).map(k => serverKpi(k)), 190);
            if(models) paintModels(models, o.models || []);
          })
          .catch(err => {
            if(my !== overviewSeq || !host.isConnected) return;
            host.innerHTML = '';
            host.appendChild(screenError(err, loadOverview, 'the metric summary'));
            if(models) models.innerHTML = '';
          });
      }

      function loadCharts(){
        const host = document.getElementById('mtCharts');
        if(!host) return;
        const my = ++chartSeq;
        host.innerHTML = `<div class="card"><div class="card-loading" style="height:220px"></div></div>
                          <div class="card"><div class="card-loading" style="height:220px"></div></div>`;
        // Both lines in one request: the server reads them off one shared
        // bucket grid, where two requests resolved the workspace twice.
        API.metrics.series(q({ metric: ['runs','cost'] }))
          .then(res => {
            if(my !== chartSeq || !host.isConnected) return;
            host.innerHTML = `${chartCard('Runs over time', res, 'runs')}${chartCard('Cost over time', res, 'cost')}`;
          })
          .catch(err => {
            if(my !== chartSeq || !host.isConnected) return;
            host.innerHTML = '';
            host.appendChild(screenError(err, loadCharts, 'the trend charts'));
          });
      }

      function chartCard(title, res, metric){
        const s = (res.series || []).find(line => line.metric === metric);
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

      /** The models table over rows already in hand: sorting and paging it asks
       *  the server nothing. Rows arrive heaviest token consumer first. */
      /* This table groups each agent's runs under the model the agent is
         REGISTERED with -- the per-agent rollup the cards above are measured
         from. Live Runs and LLM Usage group by the model each run RECORDED. The
         two agree whenever agents run the model they are registered with, and
         differ when one does not; so the column says which it is, and points to
         the screen that answers the other question. */
      function paintModels(host, rows){
        host.innerHTML = '';
        const note = document.createElement('div');
        note.className = 'small muted';
        note.style.margin = '0 0 8px';
        note.innerHTML = 'Runs grouped by each agent\u2019s <b>registered</b> model. '
          + 'For the model each run actually recorded, see <a href="#/llm-usage">LLM Usage</a>.';
        host.appendChild(note);
        const table = dataTable({
          columns: [
            { key:'model', label:'Registered Model', render:r=>`<b>${esc(r.model)}</b>` },
            { key:'agent_count', label:'Agents', align:'right', cls:'num', render:r=>fmtFull(r.agent_count) },
            { key:'runs', label:'Runs', align:'right', cls:'num', render:r=>fmtFull(r.runs) },
            { key:'tokens', label:'Tokens', align:'right', cls:'num', render:r=>r.tokens_display || dash },
            { key:'cost_usd', label:'Cost', align:'right', cls:'num', render:r=>r.cost_display || dash },
            { key:'avg_latency_seconds', label:'Avg Latency', align:'right', cls:'num', render:r=>secs(r.avg_latency_seconds) },
            { key:'success_rate_percent', label:'Success Rate', align:'right', cls:'num', render:r=>pct(r.success_rate_percent) },
            { key:'cost_share_percent', label:'Share of Cost', render:r=>r.cost_share_percent==null?dash:U.barPct(r.cost_share_percent,'orange', pct(r.cost_share_percent,0)) },
          ],
          rowId:'model', itemName:'models', pageSize:10, emptyText:'No model usage in this window',
          rows,
        });
        host.appendChild(table.el);
      }

      loadAll();
    },
  };
})();
