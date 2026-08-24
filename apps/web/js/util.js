/* Fulcrum Ops — utilities: formatting, CSV export, SVG charts */
(function(){
  'use strict';

  // ---------- time helpers ----------
  const NOW = Date.now();
  const MIN = 60000, HOUR = 3600000, DAY = 86400000;
  function agoMs(ms){ return NOW - ms; }
  function relTime(ts){
    if(ts == null) return '—';
    const d = Date.now() - ts;
    if(d < 45000) return 'just now';
    if(d < HOUR) return Math.max(1, Math.round(d/MIN)) + (Math.round(d/MIN) === 1 ? ' min ago' : ' mins ago');
    if(d < DAY) { const h = Math.round(d/HOUR); return h + (h===1?' hour ago':' hours ago'); }
    if(d < 30*DAY){ const dd = Math.round(d/DAY); return dd + (dd===1?' day ago':' days ago'); }
    return fmtDate(ts);
  }
  const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function fmtDate(ts){
    const d = new Date(ts);
    return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
  }
  function fmtDateTime(ts){
    const d = new Date(ts);
    let h = d.getHours(), m = d.getMinutes().toString().padStart(2,'0');
    const ap = h >= 12 ? 'PM' : 'AM'; h = h % 12 || 12;
    return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()} ${h}:${m} ${ap}`;
  }
  function fmtTime(ts){
    const d = new Date(ts);
    let h = d.getHours(), m = d.getMinutes().toString().padStart(2,'0'), s = d.getSeconds().toString().padStart(2,'0');
    const ap = h >= 12 ? 'PM' : 'AM'; h = h % 12 || 12;
    return `${h}:${m}:${s} ${ap}`;
  }

  // ---------- number formatting ----------
  function fmtNum(n){
    if(n == null) return '—';
    if(Math.abs(n) >= 1e9) return (n/1e9).toFixed(2).replace(/\.?0+$/,'') + 'B';
    if(Math.abs(n) >= 1e6) return (n/1e6).toFixed(n>=1e7?0:1).replace(/\.0$/,'') + 'M';
    if(Math.abs(n) >= 1e4) return (n/1e3).toFixed(0) + 'K';
    return n.toLocaleString('en-US');
  }
  function fmtFull(n){ return n == null ? '—' : n.toLocaleString('en-US'); }
  /**
   * Money, at a precision that cannot hide the amount.
   *
   * Per-run LLM spend is routinely a fraction of a cent, and rendering
   * $0.0000101 as "$0.000" reads as "this cost nothing" — which is a different
   * claim from "this cost very little". When the requested precision would
   * round a non-zero amount away, the precision widens until two significant
   * digits survive. A genuine zero still prints as zero.
   */
  function fmtMoney(n, dec){
    if(n == null) return '—';
    const size = Math.abs(n);
    if(dec == null) dec = size < 10 ? 3 : (size < 1000 ? 2 : 0);
    if(n !== 0 && size < Math.pow(10, -dec)){
      dec = Math.min(10, Math.ceil(-Math.log10(size)) + 1);
    }
    return '$' + n.toLocaleString('en-US',{minimumFractionDigits:dec, maximumFractionDigits:dec});
  }
  function fmtPct(n, dec){ return n == null ? '—' : n.toFixed(dec==null?1:dec) + '%'; }
  function fmtDur(s){
    if(s == null) return '—';
    if(s < 60) return s.toFixed(2).replace(/\.?0+$/,'') + 's';
    const m = Math.floor(s/60), r = Math.round(s%60);
    return `${m}m ${r}s`;
  }
  function fmtBytes(gb){
    if(gb >= 1024) return (gb/1024).toFixed(2) + ' TB';
    if(gb >= 1) return gb.toFixed(1).replace(/\.0$/,'') + ' GB';
    return Math.round(gb*1024) + ' MB';
  }

  function esc(s){
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function initials(name){
    return name.split(/\s+/).map(w=>w[0]).join('').slice(0,2).toUpperCase();
  }
  const AV_COLORS = ['av-purple','av-blue','av-green','av-orange','av-pink','av-cyan'];
  function avColor(name){
    let h = 0; for(let i=0;i<name.length;i++) h = (h*31 + name.charCodeAt(i)) & 0xffff;
    return AV_COLORS[h % AV_COLORS.length];
  }

  // ---------- CSV export ----------
  function downloadCSV(filename, columns, rows){
    const head = columns.map(c => `"${c.label.replace(/"/g,'""')}"`).join(',');
    const lines = rows.map(r => columns.map(c => {
      let v = typeof c.csv === 'function' ? c.csv(r) : r[c.key];
      if(v == null) v = '';
      v = String(v).replace(/<[^>]*>/g,'').replace(/"/g,'""');
      return `"${v}"`;
    }).join(','));
    const blob = new Blob(['﻿' + head + '\n' + lines.join('\n')], {type:'text/csv;charset=utf-8'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename.endsWith('.csv') ? filename : filename + '.csv';
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 400);
  }

  // ---------- SVG chart builders ----------
  const CHART_COLORS = { purple:'#6D4AEF', green:'#16A34A', orange:'#EA580C', blue:'#2563EB', amber:'#D97706', red:'#DC2626', cyan:'#0891B2', pink:'#DB2777', gray:'#94A3B8' };
  function cc(name){ return CHART_COLORS[name] || name; }

  function sparkline(points, color, w, h, opts){
    w = w||110; h = h||30; opts = opts||{};
    const min = Math.min(...points), max = Math.max(...points);
    const pad = 2, range = (max-min)||1;
    const step = (w - pad*2) / (points.length - 1);
    const pts = points.map((p,i)=> `${(pad + i*step).toFixed(1)},${(h - pad - ((p-min)/range)*(h-pad*2)).toFixed(1)}`);
    const col = cc(color);
    let fill = '';
    if(opts.fill !== false){
      fill = `<polygon points="${pad},${h-pad} ${pts.join(' ')} ${w-pad},${h-pad}" fill="${col}" opacity="0.12"/>`;
    }
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${fill}<polyline points="${pts.join(' ')}" fill="none" stroke="${col}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  }

  function lineChart(cfg){
    // cfg: {series:[{name,color,points,dashed,area}], w,h, xLabels:[], yFmt, yTicks, id}
    const w = cfg.w||560, h = cfg.h||200;
    const padL = 46, padR = 12, padT = 12, padB = 24;
    const iw = w - padL - padR, ih = h - padT - padB;
    let all = [];
    cfg.series.forEach(s => all = all.concat(s.points));
    let min = cfg.min != null ? cfg.min : Math.min(...all);
    let max = cfg.max != null ? cfg.max : Math.max(...all);
    if(min === max){ max = min + 1; }
    if(cfg.zeroBase) min = 0;
    const range = max - min;
    max += range*0.08;
    const r2 = max - min;
    const n = Math.max(...cfg.series.map(s=>s.points.length));
    const x = i => padL + (i/(n-1))*iw;
    const y = v => padT + ih - ((v-min)/r2)*ih;
    const yFmt = cfg.yFmt || (v=>fmtNum(Math.round(v)));
    const ticks = cfg.yTicks || 4;
    let gridEls = '', axisEls = '';
    for(let t=0;t<=ticks;t++){
      const v = min + (r2/ticks)*t, yy = y(v);
      gridEls += `<line x1="${padL}" y1="${yy}" x2="${w-padR}" y2="${yy}" stroke="#E6EAF2" stroke-width="1"/>`;
      axisEls += `<text x="${padL-7}" y="${yy+3.5}" text-anchor="end" class="axis-label">${yFmt(v)}</text>`;
    }
    let xEls = '';
    if(cfg.xLabels){
      const stepLbl = Math.ceil(cfg.xLabels.length / (cfg.maxXLabels||7));
      cfg.xLabels.forEach((lbl,i)=>{
        if(i % stepLbl === 0 || i === cfg.xLabels.length-1){
          xEls += `<text x="${x(i)}" y="${h-6}" text-anchor="middle" class="axis-label">${esc(lbl)}</text>`;
        }
      });
    }
    let seriesEls = '';
    cfg.series.forEach(s=>{
      const col = cc(s.color);
      const pts = s.points.map((p,i)=>`${x(i).toFixed(1)},${y(p).toFixed(1)}`).join(' ');
      if(s.area){
        seriesEls += `<polygon points="${x(0)},${y(min)} ${pts} ${x(s.points.length-1)},${y(min)}" fill="${col}" opacity="0.10"/>`;
      }
      seriesEls += `<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2" ${s.dashed?'stroke-dasharray="5 4"':''} stroke-linejoin="round" stroke-linecap="round"/>`;
      if(s.dots){
        s.points.forEach((p,i)=>{ seriesEls += `<circle cx="${x(i).toFixed(1)}" cy="${y(p).toFixed(1)}" r="2.4" fill="${col}"/>`; });
      }
    });
    return `<div class="chart-box"><svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">${gridEls}${axisEls}${xEls}${seriesEls}</svg></div>`;
  }

  function donut(cfg){
    // cfg: {segments:[{value,color,label}], size, thickness, centerVal, centerLabel}
    const size = cfg.size||150, th = cfg.thickness||16, r = (size-th)/2 - 2, cx = size/2, cy = size/2;
    const total = cfg.segments.reduce((s,x)=>s+x.value,0) || 1;
    const C = 2*Math.PI*r;
    let off = C*0.25; // start at top
    let els = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#E6EAF2" stroke-width="${th}"/>`;
    cfg.segments.forEach(seg=>{
      const frac = seg.value/total;
      const len = frac*C;
      if(len <= 0) return;
      els += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${cc(seg.color)}" stroke-width="${th}" stroke-dasharray="${Math.max(len-1.5,0.01)} ${C}" stroke-dashoffset="${off}" stroke-linecap="butt" transform="rotate(-90 ${cx} ${cy})" style="transition:stroke-dasharray .6s"/>`;
      off -= len;
    });
    const center = cfg.centerVal != null
      ? `<div class="donut-center"><div class="dc-val">${cfg.centerVal}</div>${cfg.centerLabel?`<div class="dc-label">${esc(cfg.centerLabel)}</div>`:''}</div>`
      : '';
    return `<div class="chart-box" style="width:${size}px;height:${size}px;position:relative;flex-shrink:0"><svg viewBox="0 0 ${size} ${size}">${els}</svg>${center}</div>`;
  }

  function gaugeRing(pct, color, size, label){
    return donut({segments:[{value:pct,color:color},{value:100-pct,color:'#E6EAF2'}], size:size||120, thickness:11, centerVal:Math.round(pct)+'%', centerLabel:label});
  }

  function hbars(items, opts){
    // items: [{label, value, color, display, pct}]
    opts = opts||{};
    const max = Math.max(...items.map(i=>i.value)) || 1;
    return items.map(i=>{
      const wPct = Math.max(2, (i.value/max)*100);
      return `<div class="hbar-row">
        <div class="hb-label" title="${esc(i.label)}" ${opts.labelW?`style="width:${opts.labelW}px"`:''}>${esc(i.label)}</div>
        <div class="hb-bar"><div class="bar-bg"><div class="bar-fill" style="width:${wPct}%;background:${cc(i.color||'purple')}"></div></div></div>
        ${i.display!=null?`<div class="hb-val">${i.display}</div>`:''}
        ${i.pct!=null?`<div class="hb-pct">${i.pct}</div>`:''}
      </div>`;
    }).join('');
  }

  function barPct(pct, color, num){
    const col = color || (pct >= 90 ? 'red' : pct >= 75 ? 'amber' : 'green');
    return `<div class="bar-cell"><span class="bar-num">${num != null ? num : pct.toFixed(1)+'%'}</span><div class="bar-bg"><div class="bar-fill" style="width:${Math.min(pct,100)}%;background:${cc(col)}"></div></div></div>`;
  }

  function starRating(n){
    let s = '';
    for(let i=1;i<=5;i++) s += `<span class="${i<=Math.round(n)?'':'off'}">★</span>`;
    return `<span class="stars">${s}</span>`;
  }

  window.U = { NOW, MIN, HOUR, DAY, relTime, fmtDate, fmtDateTime, fmtTime, fmtNum, fmtFull, fmtMoney, fmtPct, fmtDur, fmtBytes, esc, initials, avColor, downloadCSV, sparkline, lineChart, donut, gaugeRing, hbars, barPct, starRating, cc };
})();
