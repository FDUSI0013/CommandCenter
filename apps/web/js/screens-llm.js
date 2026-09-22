/* FD AI Command Center — LLM USAGE screen
 *
 * Which models the workspace's agents run on, how much each is used, and which
 * give the best results. One request answers the whole screen (GET /llm-usage):
 * the KPI row, the donut, the best-results panel, the trend lines and both
 * tables are views of one scan of the window, so they cannot disagree.
 *
 * Three things this screen never does:
 *   - print a zero for something that was not measured. The server sends null
 *     and the screen shows a dash, with the reason where there is room for one;
 *   - present a figure from a capped scan as a complete count. The note under
 *     the header says how much of the window was read;
 *   - blend measures into a score. "Best results" is the measured leader on
 *     each measure, with the sample it was measured on and the minimum sample
 *     a model needs to be ranked at all.
 *
 * The counting unit is the RUN (one agent execution, one row of Live Runs),
 * attributed to the model recorded on it — not LLM calls.
 */
(function(){
  'use strict';
  window.SCREENS = window.SCREENS || {};
  const { esc, fmtNum, fmtFull, fmtMoney, lineChart, donut } = U;
  const { kpiRow, kpiSkeleton, dataTable, pageHead, screenError, badge, toast } = C;

  const dash = '<span class="faint">—</span>';
  const pct = (v, digits) => v == null ? dash : Number(v).toFixed(digits == null ? 1 : digits) + '%';
  const secs = (v) => v == null ? dash : Number(v).toFixed(2) + 's';
  const money = (v) => v == null ? dash : esc(fmtMoney(v));
  const count = (v) => v == null ? dash : fmtFull(v);
  const tokens = (v) => v == null ? dash : esc(fmtNum(v));
  const rating = (v) => v == null ? dash : Number(v).toFixed(2) + '<span class="faint"> / 5</span>';
  const sample = (n, unit) => `<span class="faint small">n=${fmtFull(n || 0)}${unit ? ' ' + esc(unit) : ''}</span>`;
  const swatch = (color) =>
    `<span style="display:inline-block;width:9px;height:9px;border-radius:3px;flex-shrink:0;background:${U.cc(color || 'gray')}"></span>`;
  const when = (iso) => iso ? esc(U.fmtDateTime(new Date(iso).getTime())) : '';
  /* A section heading's icon, held to the heading's size: the stylesheet sizes
     icons inside cards and buttons, not inside .sec-title. */
  const secIcon = (name) =>
    `<span style="width:15px;height:15px;display:inline-flex;color:var(--text-dim)">${ICONS[name] || ''}</span>`;

  const WINDOWS = [['24h','Last 24 hours'],['7d','Last 7 days'],['30d','Last 30 days']];
  const ENVIRONMENTS = ['Production','Staging','UAT','Development','QA','Sandbox','DR'];
  const KPI_LABELS = ['Models in Use','Runs','Tokens','Cost'];

  /* The CSV carries every figure on its own, blank where it was not measured,
     where the table merges a few into one cell. */
  const EXPORT_COLUMNS = [
    ['label','Model'], ['providers','Providers', r => (r.providers || []).join('; ')],
    ['runs','Runs'], ['share_percent','SharePercent'],
    ['runs_model_recorded','RunsModelRecorded'], ['runs_model_from_agent','RunsModelFromAgent'],
    ['finished_runs','FinishedRuns'], ['successful_runs','SuccessfulRuns'], ['failed_runs','FailedRuns'],
    ['running_runs','RunningRuns'], ['success_rate','SuccessRatePercent'],
    ['latency_p50_seconds','LatencyP50Seconds'], ['latency_p90_seconds','LatencyP90Seconds'], ['latency_samples','LatencySamples'],
    ['input_tokens','InputTokens'], ['output_tokens','OutputTokens'], ['total_tokens','TotalTokens'], ['token_runs','TokenRuns'],
    ['cost_usd','CostUSD'], ['cost_runs','CostRuns'], ['unpriced_runs','UnpricedRuns'], ['cost_per_run','CostPerRunUSD'],
    ['cost_per_successful_run','CostPerSuccessfulRunUSD'],
    ['feedback_avg_rating','FeedbackAvgRating'], ['feedback_ratings','FeedbackRatings'],
    ['policy_flagged_runs','PolicyFlaggedRuns'], ['policy_violations','PolicyViolations'],
    ['human_escalations','HumanEscalations'], ['guardrail_triggers','GuardrailTriggers'],
    ['guardrail_blocks','GuardrailBlocks'], ['agent_handoffs','AgentHandoffs'],
    ['agents','Agents', r => (r.agents || []).map(a => `${a.name} (${a.runs})`).join('; ')],
  ].map(([key, label, csv]) => ({ key, label, csv }));

  /* The route is served by GET /llm-usage. api.js is where the console names
     its endpoints; until it names this one the screen asks by path, through
     the same client, so the request still carries the session and the
     workspace and fails with the same typed errors. */
  function fetchUsage(params){
    if(API.llmUsage && typeof API.llmUsage.get === 'function') return API.llmUsage.get(params);
    return API.get('/llm-usage', params);
  }

  /** A card that says what is missing. `title` is left out under a section heading. */
  function emptyCard(title, heading, detail, icon){
    return `<div class="card">${title ? `<div class="card-head"><div class="card-title">${esc(title)}</div></div>` : ''}
      <div class="empty-state" style="padding:30px 18px">${ICONS[icon || 'cpu'] || ''}
        <div class="es-title">${esc(heading)}</div>${detail ? `<div>${esc(detail)}</div>` : ''}</div></div>`;
  }

  /** A money formatter for a chart axis whose largest value is in `series`. */
  function costAxis(series){
    const top = Math.max(0, ...series.flatMap(s => s.points).filter(v => v != null));
    const dec = top >= 100 ? 0 : top >= 1 ? 2 : top > 0 ? Math.min(8, Math.ceil(-Math.log10(top)) + 1) : 2;
    return v => '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec });
  }

  /** How a leader's value reads, in the unit the server names. */
  function leaderValue(l){
    if(l.value == null) return dash;
    if(l.unit === 'percent') return pct(l.value);
    if(l.unit === 'rating') return rating(l.value);
    if(l.unit === 'usd') return money(l.value);
    if(l.unit === 'seconds') return secs(l.value);
    return esc(String(l.value));
  }

  SCREENS['llm-usage'] = {
    title:'LLM Usage',
    render(main){
      let window_ = '7d';
      let agentId = null;
      let environment = null;
      let trendMetric = 'runs';
      let report = null;
      let modelsTable = null;
      // Only the newest request may paint: a 30-day answer arriving after the
      // 24-hour one must not be drawn under a picker that says 24 hours.
      let seq = 0;

      main.innerHTML = `
        ${pageHead({title:'LLM Usage', sub:'Which models your agents run on, how much each is used, and which give the best results — measured from the runs themselves.',
          actions:`<select class="filter-select" id="luAgent" aria-label="Agent" style="height:34px;min-width:180px">
              <option value="">All agents</option>
            </select>
            <select class="filter-select" id="luEnv" aria-label="Environment" style="height:34px">
              <option value="">All environments</option>
              ${ENVIRONMENTS.map(e=>`<option value="${e}">${e}</option>`).join('')}
            </select>
            <select class="filter-select" id="luWindow" aria-label="Window" style="height:34px">
              ${WINDOWS.map(([v,l])=>`<option value="${v}" ${v===window_?'selected':''}>${l}</option>`).join('')}
            </select>
            <button class="btn" id="luExport">${ICONS.download}Export</button>`})}
        <div id="luNote"></div>
        <div id="luKpis" style="margin-top:12px">${kpiSkeleton(KPI_LABELS)}</div>
        <div id="luAttribution"></div>
        <div class="grid" style="margin-top:14px;grid-template-columns:repeat(auto-fit,minmax(min(100%,380px),1fr))">
          <div id="luDonut" style="min-width:0"><div class="card card-loading" style="height:250px"></div></div>
          <div id="luLeaders" style="min-width:0"><div class="card card-loading" style="height:250px"></div></div>
        </div>
        <div id="luTrend" style="margin-top:14px"><div class="card card-loading" style="height:260px"></div></div>
        <div class="sec-title" id="luModelsHead" style="margin-top:22px">${secIcon('cpu')}Models</div>
        <div id="luModels"><div class="card card-loading" style="height:180px"></div></div>
        <div id="luSignals"></div>
        <div class="sec-title" id="luAgentsHead">${secIcon('bot')}Which agents use which model</div>
        <div id="luAgents"><div class="card card-loading" style="height:160px"></div></div>`;

      document.getElementById('luWindow').addEventListener('change', e=>{ window_ = e.target.value; load(); });
      document.getElementById('luAgent').addEventListener('change', e=>{ agentId = e.target.value || null; load(); });
      document.getElementById('luEnv').addEventListener('change', e=>{ environment = e.target.value || null; load(); });
      document.getElementById('luExport').addEventListener('click', ()=>{
        if(!modelsTable || !report || !(report.models || []).length){
          toast('info','Nothing to export','No model ran in this window.');
          return;
        }
        U.downloadCSV(`llm-usage-${window_}`, EXPORT_COLUMNS, report.models);
        toast('success','Export complete', `${report.models.length} models exported to CSV.`);
      });

      // The agent picker. Losing it leaves "All agents" working, which is
      // better than losing the screen.
      API.agents.list({ page_size: 100, sort: 'name' })
        .then(page => {
          const sel = document.getElementById('luAgent');
          if(!sel) return;
          (page.items || []).forEach(a => {
            const o = document.createElement('option');
            o.value = a.id;
            o.textContent = a.name;
            sel.appendChild(o);
          });
        })
        .catch(() => {});

      function params(){
        return { window: window_, agent_id: agentId || undefined, environment: environment || undefined };
      }

      function load(){
        const my = ++seq;
        const kpis = document.getElementById('luKpis');
        if(!kpis) return;
        kpis.innerHTML = kpiSkeleton(KPI_LABELS);
        ['luNote','luAttribution','luSignals'].forEach(id => { const el = document.getElementById(id); if(el) el.innerHTML = ''; });
        [['luDonut',250],['luLeaders',250],['luTrend',260],['luModels',180],['luAgents',160]].forEach(([id, h]) => {
          const el = document.getElementById(id);
          if(el) el.innerHTML = `<div class="card card-loading" style="height:${h}px"></div>`;
        });
        fetchUsage(params())
          .then(r => {
            if(my !== seq || !kpis.isConnected) return;
            report = r;
            heads(true);
            paint(r);
          })
          .catch(err => {
            if(my !== seq || !kpis.isConnected) return;
            report = null; modelsTable = null;
            heads(false);
            kpis.innerHTML = '';
            kpis.appendChild(screenError(err, load, 'LLM usage'));
            ['luDonut','luLeaders','luTrend','luModels','luAgents'].forEach(id => {
              const el = document.getElementById(id); if(el) el.innerHTML = '';
            });
          });
      }

      /** The two section headings stand over tables; with no answer there is nothing under them. */
      function heads(shown){
        ['luModelsHead','luAgentsHead'].forEach(id => {
          const el = document.getElementById(id); if(el) el.style.display = shown ? '' : 'none';
        });
      }

      function paint(r){
        paintNote(r);
        paintKpis(r);
        paintDonut(r);
        paintLeaders(r);
        paintTrend(r);
        paintModels(r);
        paintAgents(r);
      }

      /* ---- what the numbers were read from ---- */
      function paintNote(r){
        const host = document.getElementById('luNote');
        if(!host) return;
        const scan = r.scan || {}, t = r.totals || {};
        if(!r.scan_capped){ host.innerHTML = ''; return; }
        const of = t.runs_in_window != null
          ? ` — ${t.coverage_percent != null ? pct(t.coverage_percent) + ' of ' : ''}the ${fmtFull(t.runs_in_window)} the telemetry store counts in the whole window`
          : '';
        const agents = scan.agents_total > scan.agents_scanned
          ? ` Only the ${fmtFull(scan.agents_scanned)} most recently active of ${fmtFull(scan.agents_total)} agents were read.` : '';
        // With a known edge, every per-model figure covers the same stretch for
        // every agent (the server drops what lies before it). Without one, the
        // figures are each agent's newest runs, and the note says that instead.
        const what = r.measured_from
          ? `Every per-model figure below covers the ${fmtFull(t.runs)} runs from ${when(r.measured_from)} onwards, the part of the window read whole for every agent${of}. The trend lines start there too.`
          : `Every per-model figure below is computed from each agent's newest runs, ${fmtFull(t.runs)} in all${of}, not from all of them, and there is no point in the window from which all agents were read whole.${agents}`;
        host.innerHTML = `<div class="scan-note" role="note">${ICONS.info}<span>The window held more runs than one scan reads. ${what} The window's violation, escalation and guardrail totals are counted in full.</span></div>`;
      }

      /* ---- KPI row ---- */
      function paintKpis(r){
        const host = document.getElementById('luKpis');
        const t = r.totals || {};
        host.innerHTML = kpiRow([
          { label:'Models in Use', value: fmtFull(t.models_in_use || 0), icon:'cpu', color:'purple',
            sub: t.runs_model_not_recorded ? `plus ${fmtFull(t.runs_model_not_recorded)} runs with no model recorded` : `across ${fmtFull(t.agents_with_runs || 0)} of ${fmtFull(t.agents_in_scope || 0)} agents` },
          { label:'Runs', value: fmtFull(t.runs || 0), icon:'activity', color:'blue',
            sub: !r.scan_capped ? `agent runs, not LLM calls · ${r.window_label || ''}`
              : r.measured_from ? `since ${U.fmtDateTime(new Date(r.measured_from).getTime())}${t.runs_in_window != null ? ` · ${fmtFull(t.runs_in_window)} in the whole window` : ''}`
              : t.runs_in_window != null ? `newest ${fmtFull(t.runs)} of ${fmtFull(t.runs_in_window)} in window`
              : 'newest runs only: the scan was capped' },
          { label:'Tokens', value: t.total_tokens == null ? '—' : esc(fmtNum(t.total_tokens)), icon:'layers', color:'cyan',
            sub: !t.runs ? 'no runs in this window' : t.total_tokens == null ? 'no run recorded token usage'
              : `${fmtNum(t.input_tokens)} in · ${fmtNum(t.output_tokens)} out` },
          { label:'Cost', value: t.cost_usd == null ? '—' : esc(fmtMoney(t.cost_usd)), icon:'dollar', color:'orange',
            // The store reports $0 for a model it cannot price, so a total with
            // unpriced runs in it is a floor, and says so.
            sub: !t.runs ? 'no runs in this window' : t.cost_usd == null ? 'no run recorded a cost'
              : t.unpriced_runs ? `a floor: ${fmtFull(t.unpriced_runs)} run${t.unpriced_runs === 1 ? '' : 's'} used tokens with no price`
              : (t.cost_per_successful_run == null ? 'per successful run: not measured' : `${fmtMoney(t.cost_per_successful_run)} per successful run`) },
        ], 200);

        const attribution = document.getElementById('luAttribution');
        if(attribution){
          attribution.innerHTML = t.runs_model_from_agent
            ? `<div class="faint small" style="margin-top:8px">${fmtFull(t.runs_model_from_agent)} of these runs recorded no model and are counted under the model their agent is registered with.</div>`
            : '';
        }
      }

      /** Why there is nothing to draw: no agents in scope, or agents and no runs. */
      function nothingRan(r){
        const t = r.totals || {};
        if(!t.agents_in_scope) return ['No agents match these filters', 'An agent appears here once it is registered and reporting telemetry.'];
        const agents = t.agents_in_scope === 1 ? 'The one agent in scope did not report' : `None of the ${fmtFull(t.agents_in_scope)} agents in scope reported`;
        return ['No runs in this window', `${agents} a run in the ${(r.window_label || 'selected window').toLowerCase()}.`];
      }

      /* ---- runs by model ---- */
      function paintDonut(r){
        const host = document.getElementById('luDonut');
        if(!host) return;
        const models = r.models || [];
        const total = (r.totals || {}).runs || 0;
        if(!total){
          const [h, d] = nothingRan(r);
          host.innerHTML = emptyCard('Runs by Model', h, d, 'chart');
          return;
        }
        // The chart is a picture of the legend beside it; a screen reader gets
        // the same figures in one sentence.
        const described = models.map(m => `${m.label}: ${m.runs} runs`).join('; ');
        // Model ids are long and unbroken ("anthropic.claude-3-5-sonnet-20241022-v2:0"):
        // the label truncates in its column instead of pushing the card wider
        // than its half of the grid. The full name is in the tooltip.
        host.innerHTML = `<div class="card"><div class="card-head"><div class="card-title">${ICONS.chart}Runs by Model</div>
            <div class="faint small">${esc(r.window_label || '')}</div></div>
          <div class="donut-wrap" style="min-width:0;flex-wrap:wrap"><div role="img" aria-label="${esc(`Runs by model: ${described}`)}">${donut({ segments: models.map(m => ({ value: m.runs, color: m.color })),
              size: 150, thickness: 17, centerVal: esc(fmtNum(total)), centerLabel: 'Runs' })}</div>
            <div class="legend grow" style="min-width:180px;flex:1 1 180px">${models.map(m => `<div class="legend-item" style="min-width:0">
                <span class="sw" style="background:${U.cc(m.color)}"></span>
                <span class="lg-label" title="${esc(m.label)}" style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${m.model == null ? `<i>${esc(m.label)}</i>` : esc(m.label)}</span>
                <span class="lg-val">${fmtFull(m.runs)}</span>
                <span class="lg-pct">${m.share_percent == null ? '—' : m.share_percent.toFixed(1) + '%'}</span></div>`).join('')}
            </div></div></div>`;
      }

      /* ---- best results: measured leaders, no blended score ---- */
      function paintLeaders(r){
        const host = document.getElementById('luLeaders');
        if(!host) return;
        const leaders = r.leaders || [];
        if(!((r.totals || {}).runs)){
          host.innerHTML = emptyCard('Best Results', 'Nothing to rank', 'A model is ranked once it has runs in the window.', 'target');
          return;
        }
        const rows = leaders.map(l => {
          const main = l.model_label
            ? `<b>${esc(l.model_label)}</b>${l.tied_with && l.tied_with.length ? ` <span class="faint small">tied with ${esc(l.tied_with.join(', '))}</span>` : ''}`
            : `<span class="faint">No leader</span>`;
          const facts = l.model_label
            ? `measured on ${fmtFull(l.sample)} ${esc(l.sample_unit)} · ${fmtFull(l.eligible_models)} model${l.eligible_models === 1 ? '' : 's'} ranked`
            : '';
          return `<div style="padding:10px 0;border-top:1px solid var(--border)">
              <div class="flex between"><span class="muted small" style="font-weight:600;text-transform:uppercase;letter-spacing:.03em">${esc(l.label)}</span>
                <span class="score-pill">${leaderValue(l)}</span></div>
              <div style="margin-top:3px;overflow-wrap:anywhere">${main}</div>
              ${facts ? `<div class="faint small" style="margin-top:2px">${facts}</div>` : ''}
              ${l.note ? `<div class="faint small" style="margin-top:2px">${esc(l.note)}</div>` : ''}
            </div>`;
        }).join('');
        host.innerHTML = `<div class="card"><div class="card-head"><div class="card-title">${ICONS.target}Best Results</div></div>
          ${rows}
          <div class="faint small" style="padding-top:10px;border-top:1px solid var(--border)">
            Each measure is ranked on its own, only among models with at least ${fmtFull(r.leader_min_runs)} runs
            (${fmtFull(r.leader_min_ratings)} ratings for feedback). Runs with no recorded model are not ranked.
            There is no combined score.</div></div>`;
      }

      /* ---- runs, tokens or cost per model over time ---- */
      function paintTrend(r){
        const host = document.getElementById('luTrend');
        if(!host) return;
        const series = r.series || {};
        const buckets = series.buckets || [];
        if(!((r.totals || {}).runs) || !buckets.length){
          const [h, d] = nothingRan(r);
          host.innerHTML = emptyCard('Usage over Time', h, d, 'trendUp');
          return;
        }
        // A bucket the capped scan did not read whole is not a measurement,
        // so the lines start at the first bucket that was.
        const first = Math.max(0, buckets.findIndex(b => !b.partial));
        const shown = buckets.findIndex(b => !b.partial) === -1 ? [] : buckets.slice(first);
        const lines = (series.models || []).slice(0, 8);
        const drawable = [], skipped = [];
        lines.forEach(m => {
          const values = (m[trendMetric] || []).slice(first);
          if(values.some(v => v == null)) skipped.push(m.label);
          else drawable.push({ name: m.label, color: m.color, points: values });
        });
        const unit = { runs:'Runs', tokens:'Tokens', cost:'Cost' }[trendMetric];
        const picker = `<select class="filter-select" id="luTrendMetric" aria-label="Measure to chart" style="height:28px">
            ${['runs','tokens','cost'].map(k => `<option value="${k}" ${k===trendMetric?'selected':''}>${({runs:'Runs',tokens:'Tokens',cost:'Cost'})[k]}</option>`).join('')}
          </select>`;
        let body;
        if(shown.length < 2){
          body = `<div class="empty-state" style="padding:30px 18px">${ICONS.trendUp}<div class="es-title">Not enough of the window was read whole to draw a trend</div>
            <div>The scan was capped; see the note above.</div></div>`;
        } else if(!drawable.length){
          body = `<div class="empty-state" style="padding:30px 18px">${ICONS.trendUp}<div class="es-title">No model recorded ${esc(unit.toLowerCase())} for every bucket it ran in</div></div>`;
        } else {
          const chartName = `${unit} by model, ${series.interval || ''} from ${shown[0].label} to ${shown[shown.length - 1].label} UTC: ${drawable.map(s => s.name).join(', ')}`;
          body = `<div style="padding:6px 4px" role="img" aria-label="${esc(chartName)}">${lineChart({
              series: drawable,
              xLabels: shown.map(b => b.label),
              zeroBase: true,
              // A handful of runs an hour needs its fractions, or the axis reads 0, 1, 2, 2, 3.
              // One precision for the whole axis, enough for its largest tick to
              // keep two significant digits: fmtMoney widens tick by tick, which
              // printed "$0.00, $0.0081, $0.02, $0.02" up one axis.
              yFmt: trendMetric === 'cost' ? costAxis(drawable)
                : (v => Math.abs(v) < 10 ? String(Math.round(v * 10) / 10) : fmtNum(Math.round(v))),
              w: 900, h: 230, maxXLabels: 9,
            })}</div>
            <div class="legend inline" style="padding:4px 6px 0">${drawable.map(s =>
              `<div class="legend-item" style="min-width:0"><span class="sw" style="background:${U.cc(s.color)}"></span><span class="lg-label" style="overflow-wrap:anywhere">${esc(s.name)}</span></div>`).join('')}</div>`;
        }
        const notes = [];
        if(skipped.length) notes.push(`Not drawn: ${esc(skipped.join(', '))} — some of their runs recorded no ${esc(unit.toLowerCase())}, so a line would understate them.`);
        if((series.models || []).length > lines.length) notes.push(`Lines are drawn for ${lines.length} of the ${(series.models || []).length} models: the named models with the most runs. The table below has all of them.`);
        host.innerHTML = `<div class="card"><div class="card-head"><div class="card-title">${ICONS.trendUp}${esc(unit)} by Model over Time</div>
            <div class="flex"><span class="faint small">${esc(series.interval || '')}${series.interval ? ' · UTC' : ''}</span>${picker}</div></div>
          ${body}
          ${notes.map(n => `<div class="faint small" style="margin-top:6px">${n}</div>`).join('')}</div>`;
        const sel = document.getElementById('luTrendMetric');
        if(sel) sel.addEventListener('change', e => { trendMetric = e.target.value; if(report) paintTrend(report); });
      }

      /* ---- the per-model table ---- */
      function paintModels(r){
        const host = document.getElementById('luModels');
        if(!host) return;
        const rows = r.models || [];
        modelsTable = null;
        if(!rows.length){
          const [h, d] = nothingRan(r);
          host.innerHTML = emptyCard(null, h, d, 'cpu');
          return;
        }
        host.innerHTML = '';
        modelsTable = dataTable({
          columns: [
            { key:'label', label:'Model',
              render:m=>`<div class="flex" style="gap:8px">${swatch(m.color)}<div style="min-width:0">
                  <div class="cell-main">${m.model == null ? `<i>${esc(m.label)}</i>` : esc(m.label)}</div>
                  <div class="cell-sub">${m.providers && m.providers.length ? esc(m.providers.join(', ')) : 'provider not recorded'}</div></div></div>` },
            { key:'runs', label:'Runs', align:'right', cls:'num',
              render:m=>`${fmtFull(m.runs)}<div class="cell-sub">${pct(m.share_percent)} of runs</div>${m.runs_model_from_agent
                ? `<div class="cell-sub" title="Recorded no model; counted under the agent's registered model">${fmtFull(m.runs_model_from_agent)} by registration</div>` : ''}` },
            { key:'success_rate', label:'Success Rate', align:'right', cls:'num',
              render:m=>`${pct(m.success_rate)}<div>${sample(m.finished_runs, 'finished')}</div>` },
            { key:'latency_p50_seconds', label:'Latency p50 / p90', align:'right', cls:'num',
              render:m=>`${secs(m.latency_p50_seconds)} <span class="faint">/</span> ${secs(m.latency_p90_seconds)}<div>${sample(m.latency_samples, 'timed')}</div>` },
            { key:'total_tokens', label:'Tokens', align:'right', cls:'num',
              render:m=>m.total_tokens == null
                ? `<span class="faint" title="No run on this model recorded token usage">—</span>`
                : `${tokens(m.total_tokens)}<div class="cell-sub">${tokens(m.input_tokens)} in · ${tokens(m.output_tokens)} out</div>` },
            { key:'cost_usd', label:'Cost', align:'right', cls:'num',
              render:m=>`${money(m.cost_usd)}${m.cost_per_run == null ? '' : `<div class="cell-sub">${money(m.cost_per_run)} per run</div>`}${m.unpriced_runs
                ? `<div class="cell-sub" title="These runs used tokens and the store recorded $0: it has no price for them, so the cost shown is a floor">${fmtFull(m.unpriced_runs)} unpriced</div>` : ''}` },
            { key:'cost_per_successful_run', label:'Cost / Success', align:'right', cls:'num',
              render:m=>m.cost_per_successful_run == null && m.successful_runs
                ? `<span class="faint" title="Not measured: a finished run on this model recorded no cost, or had no price">—</span>`
                : money(m.cost_per_successful_run) },
            { key:'feedback_avg_rating', label:'Rating', align:'right', cls:'num',
              render:m=>m.feedback_ratings ? `${rating(m.feedback_avg_rating)}<div>${sample(m.feedback_ratings, 'rated')}</div>` : dash },
            { key:'policy_violations', label:'Violations', align:'right', cls:'num',
              render:m=>`${count(m.policy_violations)}${m.human_escalations ? `<div class="cell-sub">${fmtFull(m.human_escalations)} escalated</div>` : ''}` },
            { key:'guardrail_triggers', label:'Guardrail Triggers', align:'right', cls:'num',
              render:m=>`${count(m.guardrail_triggers)}${m.guardrail_blocks ? `<div class="cell-sub">${fmtFull(m.guardrail_blocks)} blocked</div>` : ''}` },
            { key:'agents', label:'Agents', sortVal:m=>(m.agents || []).length,
              render:m=>{
                const list = m.agents || [];
                const names = list.slice(0, 2).map(a => esc(a.name)).join(', ');
                const more = list.length > 2 ? ` <span class="badge bg-gray">+${list.length - 2}</span>` : '';
                return `<span title="${esc(list.map(a=>`${a.name}: ${a.runs} runs`).join('\n'))}">${names || dash}${more}</span>`;
              } },
          ],
          rowId:'label', itemName:'models', pageSize:10,
          rows,
        });
        host.appendChild(modelsTable.el);

        const t = r.totals || {};
        const signals = document.getElementById('luSignals');
        if(signals){
          // The report says which filters it answered; the pickers may already
          // have moved on to the next question.
          const narrowed = (r.agent_ids || []).length || r.environment;
          const scope = narrowed
            ? 'for the selected agents'
            : 'across the workspace, counted as the Policy Center counts them';
          const lines = [
            `Violations, human escalations and guardrail triggers are the records that name one of these runs by trace id: ${fmtFull(t.policy_violations_attributed)} of the ${fmtFull(t.policy_violations_in_window)} violation records, ${fmtFull(t.human_escalations_attributed)} of the ${fmtFull(t.human_escalations_in_window)} human escalations and ${fmtFull(t.guardrail_triggers_attributed)} of the ${fmtFull(t.guardrail_triggers_in_window)} guardrail events in the ${esc((r.window_label || 'selected window').toLowerCase())} ${scope}. The rest name no run that was read — a blocked request never stored, an offline check, or a run outside the scan.`,
            t.feedback_ratings
              ? `Rating is the mean 1–5 rating on the Feedback &amp; Quality Loop records that name a run on the model; ${fmtFull(t.feedback_ratings)} ratings in all.`
              : 'No run in this window has a feedback rating, so the Rating column is empty.',
          ];
          signals.innerHTML = lines.map(l => `<div class="faint small" style="margin-top:8px">${l}</div>`).join('');
        }
      }

      /* ---- which agents use which model ---- */
      function paintAgents(r){
        const host = document.getElementById('luAgents');
        if(!host) return;
        const rows = (r.agents || []).map(a => Object.assign({ key: `${a.agent_id}::${a.model_label}` }, a));
        if(!rows.length){
          const [h, d] = nothingRan(r);
          host.innerHTML = emptyCard(null, h, d, 'bot');
          return;
        }
        host.innerHTML = '';
        const table = dataTable({
          columns: [
            { key:'agent_name', label:'Agent', render:a=>`<div class="cell-main">${esc(a.agent_name)}</div>` },
            { key:'environment', label:'Environment', render:a=>a.environment ? badge(a.environment) : dash },
            { key:'model_label', label:'Model',
              render:a=>`${a.model == null ? `<i>${esc(a.model_label)}</i>` : esc(a.model_label)}${
                a.matches_configuration === false ? ` <span class="badge bg-amber" title="The agent is registered with ${esc(a.configured_model)}">not registered model</span>` : ''}` },
            { key:'configured_model', label:'Registered Model', render:a=>a.configured_model ? esc(a.configured_model) : dash },
            { key:'runs', label:'Runs', align:'right', cls:'num', render:a=>fmtFull(a.runs) },
            { key:'share_of_agent_percent', label:"Share of Agent's Runs", align:'right', cls:'num', render:a=>pct(a.share_of_agent_percent) },
            { key:'success_rate', label:'Success Rate', align:'right', cls:'num',
              render:a=>`${pct(a.success_rate)}<div>${sample(a.finished_runs, 'finished')}</div>` },
            { key:'total_tokens', label:'Tokens', align:'right', cls:'num', render:a=>tokens(a.total_tokens) },
            { key:'cost_usd', label:'Cost', align:'right', cls:'num', render:a=>money(a.cost_usd) },
          ],
          rowId:'key', itemName:'agent-model pairs', pageSize:10,
          searchKeys:['agent_name','model_label','configured_model','environment'],
          rows,
        });
        host.appendChild(table.el);
      }

      load();
    },
  };
})();
