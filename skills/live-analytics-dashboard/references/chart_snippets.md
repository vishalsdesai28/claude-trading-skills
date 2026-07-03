# CSP-Safe Chart Snippets

Ready-to-adapt visual widgets. All are CSP-safe: no inline `on*` handlers, no
`eval`, events via `addEventListener`. Match the dark theme the built-in
templates use.

## Which visuals go where

| Target | Allowed | Why |
|--------|---------|-----|
| **Artifact / Tier-1 static** | Inline **CSS bars, inline SVG**, HTML tables/KPI cards | The Artifact CSP blocks external CDN scripts — a `<script src="https://cdn…">` never loads. Everything must be inlined. |
| **Tier-2 FastAPI (local serve)** | The above **plus CDN chart libraries** (Chart.js, Plotly) | A normal localhost browser has no Artifact CSP, so CDN `<script src>` loads fine. |

The generated dashboards use KPI cards + a sortable table only, which are
Artifact-safe by construction. Add a chart from below when the report warrants it.

## Theme tokens

| Token | Value |
|-------|-------|
| Page bg | `#0f1117` |
| Card bg | `#1a1d27` |
| Text | `#e5e7eb` |
| Muted | `#9ca3af` |
| Gain | `#10b981` |
| Loss | `#ef4444` |
| Accent | `#3b82f6` |
| Border | `#2d3748` |

## 1. CSS bar row — Artifact-safe, no library

Good for grade distributions, sector weights, breadth ratios. Pure CSS width.

```html
<div id="bars" class="card"></div>
<script>
// DATA: [{label:'A', value:12, max:20}, ...]
function renderBars(rows){
  var host = document.getElementById('bars');
  host.textContent = '';
  var max = Math.max.apply(null, rows.map(function(r){ return r.max || r.value; }));
  rows.forEach(function(r){
    var wrap = document.createElement('div'); wrap.className = 'bar-wrap';
    var lab = document.createElement('span'); lab.className = 'bar-label'; lab.textContent = r.label;
    var track = document.createElement('div'); track.className = 'bar-track';
    var fill = document.createElement('div'); fill.className = 'bar-fill';
    fill.style.width = (100 * r.value / (max || 1)).toFixed(1) + '%';
    var val = document.createElement('span'); val.className = 'bar-val'; val.textContent = r.value;
    track.appendChild(fill); wrap.appendChild(lab); wrap.appendChild(track); wrap.appendChild(val);
    host.appendChild(wrap);
  });
}
document.addEventListener('DOMContentLoaded', function(){ renderBars(DATA); });
</script>
```

```css
.bar-wrap{display:grid;grid-template-columns:80px 1fr 60px;align-items:center;gap:10px;margin:6px 0}
.bar-label{color:#9ca3af;font-size:0.8rem}
.bar-track{background:#2d3748;border-radius:4px;height:14px;overflow:hidden}
.bar-fill{background:#3b82f6;height:100%}
.bar-val{color:#e5e7eb;font-size:0.8rem;text-align:right}
```

## 2. Inline SVG sparkline — Artifact-safe, no library

Good for a per-row price/score trend. Build the `points` string in JS.

```html
<svg class="spark" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true">
  <polyline id="sparkLine" fill="none" stroke="#3b82f6" stroke-width="2" points=""></polyline>
</svg>
<script>
function renderSpark(series){ // series: [number, ...]
  var min = Math.min.apply(null, series), max = Math.max.apply(null, series);
  var span = (max - min) || 1, step = 100 / (series.length - 1 || 1);
  var pts = series.map(function(v, i){
    var y = 24 - ((v - min) / span) * 24;
    return (i * step).toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  document.getElementById('sparkLine').setAttribute('points', pts);
}
document.addEventListener('DOMContentLoaded', function(){ renderSpark(DATA.series); });
</script>
```

## 3. Chart.js line — Tier-2 (local serve) only

CDN scripts load only when served from localhost (not in an Artifact). Note the
listener is attached with `addEventListener`, never an inline `onload`.

```html
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<div class="card"><canvas id="priceChart"></canvas></div>
<script>
function renderPrice(data){ // {labels:[...], prices:[...]}
  var ctx = document.getElementById('priceChart').getContext('2d');
  new Chart(ctx, {
    type: 'line',
    data: { labels: data.labels, datasets: [{ data: data.prices, borderColor: '#3b82f6', borderWidth: 2, pointRadius: 0, tension: 0.1 }] },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#9ca3af', maxTicksLimit: 8 }, grid: { color: 'rgba(45,55,72,0.5)' } },
        y: { ticks: { color: '#9ca3af', callback: function(v){ return '$' + v.toFixed(0); } }, grid: { color: 'rgba(45,55,72,0.5)' } }
      }
    }
  });
}
document.addEventListener('DOMContentLoaded', function(){ renderPrice(DATA); });
</script>
```

After adding any snippet, re-run the gate: `python3 scripts/csp_check.py <output>`.
