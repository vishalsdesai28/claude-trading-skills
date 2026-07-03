"""Build a live-analytics dashboard from another skill's ``reports/*.json``.

Renders a skill's JSON output (watchlist / breadth / portfolio monitors, or any
generic record set) into one of two serve tiers:

  * ``static``  -- one self-contained HTML file (no external scripts). Safe to
    open locally, serve with ``python3 -m http.server``, or hand to the
    claude.ai Artifact tool. Use for **snapshots**.
  * ``fastapi`` -- a tiny FastAPI app that re-reads the report JSON on every
    ``/api/data`` request, plus a polling HTML page. Use only when the data
    genuinely refreshes intraday and the Artifact/static path cannot poll a
    live backend (the Artifact CSP blocks ``fetch`` to external hosts).

A third ``spa`` tier (client-side routing / multi-page React) is intentionally
*not* scaffolded here -- escalate manually only when needed (references/serve_tiers.md).

Every generated HTML file passes the ``csp_check`` gate before the build is
reported as complete; a violation fails the build. Pure stdlib + ``csp_check``
only -- no network, no FastAPI import at build time (the server file is written
as text). Tests run fully offline against committed fixtures.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from csp_check import CspViolationError, assert_csp_safe

# ── Fixed conventions ────────────────────────────────────────────────────────
DEFAULT_PORT = 8770
PORT_RANGE = range(8770, 8780)  # 8770-8779 reserved for this skill's dashboards
DEFAULT_OUTPUT_ROOT = Path("reports/dashboard")

# Keys whose value is the primary record array, in priority order.
RECORD_KEYS = (
    "candidates",
    "watchlist",
    "positions",
    "holdings",
    "signals",
    "rows",
    "records",
    "results",
    "items",
)
# Top-level keys treated as metadata (never rendered as a KPI card).
META_KEYS = frozenset(
    {
        "generated_at",
        "as_of",
        "timestamp",
        "date",
        "report_date",
        "source",
        "skill",
        "version",
        "schema_version",
        "run_id",
        "meta",
    }
)
CURRENCY_HINTS = (
    "price",
    "value",
    "cost",
    "cap",
    "close",
    "open",
    "high",
    "low",
    "nav",
    "equity",
    "proceeds",
    "dollar",
)
PERCENT_HINTS = ("pct", "percent", "change", "return", "weight", "yield", "gain", "chg", "alloc")
MAX_KPIS = 8


# ── Report loading & shape detection ─────────────────────────────────────────
def load_report(path: str | Path) -> object:
    """Load a report JSON file (dict or list)."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _find_records(data: object) -> tuple[str | None, list[dict]]:
    """Return ``(key, records)`` for the primary array of dict records."""
    if isinstance(data, list):
        return None, [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return None, []
    for k in RECORD_KEYS:
        v = data.get(k)
        if isinstance(v, list) and v and all(isinstance(r, dict) for r in v):
            return k, v
    best_key: str | None = None
    best: list[dict] = []
    for k, v in data.items():
        if isinstance(v, list) and v and all(isinstance(r, dict) for r in v) and len(v) > len(best):
            best_key, best = k, v
    return best_key, best


def detect_layout(data: object) -> str:
    """Classify the report as watchlist / breadth / portfolio / generic."""
    if not isinstance(data, dict):
        return "generic"
    keys = {k.lower() for k in data.keys()}
    key, records = _find_records(data)
    kl = (key or "").lower()
    if kl in ("positions", "holdings") or {"positions", "holdings"} & keys:
        return "portfolio"
    if kl in ("candidates", "watchlist") or {"candidates", "watchlist"} & keys:
        return "watchlist"
    breadth_terms = (
        "advancer",
        "decliner",
        "breadth",
        "advance_decline",
        "new_high",
        "new_low",
        "pct_above",
        "mcclellan",
        "up_volume",
        "down_volume",
    )
    if any(any(term in k for term in breadth_terms) for k in keys):
        return "breadth"
    if records and any(("ticker" in r or "symbol" in r) for r in records):
        return "watchlist"
    return "generic"


# ── View-model helpers ───────────────────────────────────────────────────────
def _humanize(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip().title()


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _infer_type(key: str, values: list[object]) -> str:
    kl = key.lower()
    non_null = [v for v in values if v is not None]
    if not non_null or not all(_is_number(v) for v in non_null):
        return "string"
    if any(h in kl for h in PERCENT_HINTS):
        return "percent"
    if any(h in kl for h in CURRENCY_HINTS):
        return "currency"
    return "number"


def _columns_from_records(records: list[dict]) -> list[dict]:
    seen: list[str] = []
    for r in records:
        for k in r.keys():
            if k not in seen:
                seen.append(k)
    cols: list[dict] = []
    for k in seen:
        vals = [r.get(k) for r in records]
        cols.append({"key": k, "label": _humanize(k), "type": _infer_type(k, vals)})
    id_priority = {"ticker": 0, "symbol": 0, "name": 1, "company": 1}
    cols.sort(
        key=lambda c: id_priority.get(c["key"].lower(), 5)
    )  # stable -> preserves first-seen order
    return cols


def _abbrev_currency(v: float) -> str:
    a = abs(v)
    if a >= 1e12:
        return f"${v / 1e12:.2f}T"
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.2f}M"
    if a >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:.2f}"


def _format_scalar(key: str, v: object) -> str:
    kl = key.lower()
    if _is_number(v):
        if any(h in kl for h in PERCENT_HINTS):
            return f"{v:+.2f}%"
        if any(h in kl for h in CURRENCY_HINTS):
            return _abbrev_currency(float(v))
        return f"{v:,.2f}" if isinstance(v, float) else f"{v:,}"
    return str(v)


def _scalar_direction(key: str, v: object) -> str:
    if _is_number(v) and any(h in key.lower() for h in PERCENT_HINTS):
        return "positive" if v > 0 else "negative" if v < 0 else "neutral"
    return "neutral"


def _kpis_from_top_level(data: object, exclude: frozenset[str] | set[str]) -> list[dict]:
    if not isinstance(data, dict):
        return []
    kpis: list[dict] = []
    for k, v in data.items():
        if k in exclude or isinstance(v, (dict, list)) or v is None:
            continue
        if isinstance(v, bool):
            kpis.append(
                {"label": _humanize(k), "value": "Yes" if v else "No", "direction": "neutral"}
            )
            continue
        kpis.append(
            {
                "label": _humanize(k),
                "value": _format_scalar(k, v),
                "direction": _scalar_direction(k, v),
            }
        )
    return kpis


_TITLES = {
    "watchlist": "Watchlist Monitor",
    "breadth": "Market Breadth Monitor",
    "portfolio": "Portfolio Monitor",
    "generic": "Analytics Dashboard",
}


def _default_title(layout: str, data: object) -> str:
    base = _TITLES.get(layout, "Analytics Dashboard")
    src = None
    if isinstance(data, dict):
        src = data.get("source") or data.get("skill")
    return f"{base} — {src}" if src else base


def build_view_model(
    data: object,
    layout: str | None = None,
    title: str | None = None,
    source_report: str | None = None,
) -> dict:
    """Normalize any report into the view model the HTML templates render."""
    layout = layout or detect_layout(data)
    key, records = _find_records(data)
    columns = _columns_from_records(records) if records else []
    exclude = set(RECORD_KEYS) | set(META_KEYS)
    if key:
        exclude.add(key)
    kpis = _kpis_from_top_level(data, exclude)[:MAX_KPIS]
    generated_at = ""
    if isinstance(data, dict):
        for mk in ("generated_at", "as_of", "report_date", "date", "timestamp"):
            if data.get(mk):
                generated_at = str(data[mk])
                break
    return {
        "title": title or _default_title(layout, data),
        "layout": layout,
        "generated_at": generated_at,
        "source_report": source_report or "",
        "kpis": kpis,
        "table": {"columns": columns, "rows": records},
        "record_count": len(records),
    }


# ── HTML rendering (CSP-safe, self-contained) ────────────────────────────────
def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _embed_json(obj: object) -> str:
    """JSON for embedding inside a <script> block (breakout-safe)."""
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(chr(0x2028), "\\u2028")
        .replace(chr(0x2029), "\\u2029")
    )


_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0f1117;color:#e5e7eb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:20px}
h1{font-size:1.5rem;margin-bottom:4px}
.sub{color:#9ca3af;font-size:0.8rem;margin-bottom:14px}
#status{color:#9ca3af;font-size:0.75rem;margin-bottom:16px;min-height:1em}
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}
.kpi-card{background:#1a1d27;border:1px solid #2d3748;border-radius:8px;padding:16px}
.kpi-label{font-size:0.72rem;color:#9ca3af;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px}
.kpi-value{font-size:1.6rem;font-weight:700;line-height:1.2}
.kpi-value.positive{color:#10b981}
.kpi-value.negative{color:#ef4444}
.card{background:#1a1d27;border:1px solid #2d3748;border-radius:8px;padding:20px;overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px 12px;font-size:0.72rem;color:#9ca3af;text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid #2d3748;cursor:pointer;user-select:none;white-space:nowrap}
th.num,td.num{text-align:right}
td{padding:10px 12px;font-size:0.875rem;border-bottom:1px solid rgba(45,55,72,0.4)}
tbody tr:nth-child(even){background:rgba(37,42,54,0.4)}
tbody tr:hover{background:#252a36}
.positive{color:#10b981}
.negative{color:#ef4444}
.empty{color:#9ca3af;padding:16px}
</style>
</head>
<body>
<h1 id="title">__TITLE__</h1>
<div class="sub" id="generated"></div>
<div id="status"></div>
<div class="kpi-row" id="kpiRow"></div>
<div class="card">
<table id="dataTable"><thead id="thead"></thead><tbody id="tbody"></tbody></table>
<div class="empty" id="emptyMsg" style="display:none">No tabular records in this report.</div>
</div>
"""

# Shared render logic for both tiers. CSP-safe: no inline handlers, no eval,
# events bound via addEventListener, DOM built with createElement/textContent.
_RENDER_JS = """
function fmtCurrency(v){var a=Math.abs(v);if(a>=1e12)return '$'+(v/1e12).toFixed(2)+'T';if(a>=1e9)return '$'+(v/1e9).toFixed(2)+'B';if(a>=1e6)return '$'+(v/1e6).toFixed(2)+'M';if(a>=1e3)return '$'+(v/1e3).toFixed(1)+'K';return '$'+v.toFixed(2);}
function fmtCell(v,type){
  if(v===null||v===undefined)return {text:'-',cls:''};
  if(typeof v==='object')return {text:JSON.stringify(v),cls:''};
  if(type==='currency'&&typeof v==='number')return {text:fmtCurrency(v),cls:''};
  if(type==='percent'&&typeof v==='number'){var s=v>=0?'+':'';return {text:s+v.toFixed(2)+'%',cls:v>=0?'positive':'negative'};}
  if(type==='number'&&typeof v==='number')return {text:v.toLocaleString('en-US',{maximumFractionDigits:2}),cls:''};
  return {text:String(v),cls:''};
}
var SORT={key:null,asc:true};
var CURRENT=null;
function renderKpis(kpis){
  var row=document.getElementById('kpiRow');
  row.textContent='';
  (kpis||[]).forEach(function(k){
    var card=document.createElement('div');card.className='kpi-card';
    var lab=document.createElement('div');lab.className='kpi-label';lab.textContent=k.label;
    var val=document.createElement('div');val.className='kpi-value '+(k.direction||'neutral');val.textContent=k.value;
    card.appendChild(lab);card.appendChild(val);row.appendChild(card);
  });
}
function renderTable(table){
  var thead=document.getElementById('thead');
  var tbody=document.getElementById('tbody');
  var empty=document.getElementById('emptyMsg');
  var cols=(table&&table.columns)||[];
  var rows=(table&&table.rows)||[];
  if(!cols.length||!rows.length){thead.textContent='';tbody.textContent='';empty.style.display='block';return;}
  empty.style.display='none';
  var sorted=rows.slice();
  if(SORT.key){
    sorted.sort(function(a,b){
      var av=a[SORT.key],bv=b[SORT.key];
      if(av===null||av===undefined)return 1;
      if(bv===null||bv===undefined)return -1;
      var cmp=(typeof av==='string')?av.localeCompare(bv):(av-bv);
      return SORT.asc?cmp:-cmp;
    });
  }
  var htr=document.createElement('tr');
  cols.forEach(function(c){
    var th=document.createElement('th');
    if(c.type!=='string')th.className='num';
    th.dataset.key=c.key;
    var arrow=(SORT.key===c.key)?(SORT.asc?' \\u25B2':' \\u25BC'):'';
    th.textContent=c.label+arrow;
    htr.appendChild(th);
  });
  thead.textContent='';thead.appendChild(htr);
  var frag=document.createDocumentFragment();
  sorted.forEach(function(r){
    var tr=document.createElement('tr');
    cols.forEach(function(c){
      var td=document.createElement('td');
      if(c.type!=='string')td.className='num';
      var f=fmtCell(r[c.key],c.type);
      td.textContent=f.text;
      if(f.cls)td.classList.add(f.cls);
      tr.appendChild(td);
    });
    frag.appendChild(tr);
  });
  tbody.textContent='';tbody.appendChild(frag);
  if(!thead.dataset.bound){thead.addEventListener('click',onHeaderClick);thead.dataset.bound='1';}
}
function onHeaderClick(e){
  var th=e.target.closest('th');
  if(!th||!th.dataset.key)return;
  var key=th.dataset.key;
  if(SORT.key===key){SORT.asc=!SORT.asc;}else{SORT={key:key,asc:true};}
  if(CURRENT)renderTable(CURRENT.table);
}
function setStatus(msg){var s=document.getElementById('status');if(s)s.textContent=msg;}
function renderDashboard(vm){
  CURRENT=vm;
  document.getElementById('title').textContent=vm.title||'Dashboard';
  var parts=[];
  if(vm.generated_at)parts.push('As of '+vm.generated_at);
  if(vm.record_count!==undefined)parts.push(vm.record_count+' records');
  if(vm.source_report)parts.push('Source: '+vm.source_report);
  document.getElementById('generated').textContent=parts.join('  \\u00B7  ');
  renderKpis(vm.kpis);
  renderTable(vm.table);
}
"""

_STATIC_BOOT = """<script>
const DATA = __DATA__;
__RENDER_JS__
document.addEventListener('DOMContentLoaded', function(){ renderDashboard(DATA); });
</script>
</body>
</html>
"""

_FASTAPI_BOOT = """<script>
__RENDER_JS__
var POLL_MS = __POLL_MS__;
var API = '__API__';
function refresh(){
  fetch(API, {cache:'no-store'}).then(function(r){
    if(!r.ok) throw new Error('HTTP '+r.status);
    return r.json();
  }).then(function(vm){
    if(vm && vm.error){ setStatus('Server: '+vm.error); }
    else { setStatus('Updated '+new Date().toLocaleTimeString()); }
    renderDashboard(vm);
  }).catch(function(err){
    setStatus('Refresh error: '+err.message);
  });
}
document.addEventListener('DOMContentLoaded', function(){ refresh(); setInterval(refresh, POLL_MS); });
</script>
</body>
</html>
"""


def render_static_html(view_model: dict) -> str:
    """Self-contained snapshot page with the view model embedded inline."""
    head = _HEAD.replace("__TITLE__", _html_escape(view_model.get("title", "Dashboard")))
    boot = _STATIC_BOOT.replace("__RENDER_JS__", _RENDER_JS).replace(
        "__DATA__", _embed_json(view_model)
    )
    return head + boot


def render_fastapi_index_html(
    view_model: dict, poll_seconds: int = 30, api_path: str = "/api/data"
) -> str:
    """Polling page that fetches the view model from the FastAPI backend."""
    head = _HEAD.replace("__TITLE__", _html_escape(view_model.get("title", "Dashboard")))
    boot = (
        _FASTAPI_BOOT.replace("__RENDER_JS__", _RENDER_JS)
        .replace("__POLL_MS__", str(int(poll_seconds) * 1000))
        .replace("__API__", api_path)
    )
    return head + boot


# ── FastAPI server + launcher templates (written as text; never imported here) ─
_SERVER_MAIN = '''"""FastAPI live dashboard server (generated by live-analytics-dashboard).

Re-reads the source report JSON on every request to the data endpoint so the
polling page always shows fresh numbers. Bind host is 0.0.0.0; the port comes
from $PORT or the value baked in at build time. Override the report path with
the DASHBOARD_REPORT environment variable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_dashboard import build_view_model, load_report  # noqa: E402

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE.parent / "static"
DEFAULT_REPORT = __REPORT_LIT__
LAYOUT = (__LAYOUT_LIT__) or None
TITLE = (__TITLE_LIT__) or None
API_PATH = __API_LIT__

app = FastAPI(title=TITLE or "Live Analytics Dashboard")


def _report_path() -> Path:
    raw = os.environ.get("DASHBOARD_REPORT", DEFAULT_REPORT)
    p = Path(raw)
    return p if p.is_absolute() else (Path.cwd() / p)


@app.get(API_PATH)
def api_data() -> JSONResponse:
    path = _report_path()
    if not path.exists():
        return JSONResponse(
            {
                "title": TITLE or "Dashboard",
                "error": f"Report not found: {path}",
                "kpis": [],
                "table": {"columns": [], "rows": []},
                "record_count": 0,
            }
        )
    vm = build_view_model(load_report(path), layout=LAYOUT, title=TITLE, source_report=path.name)
    return JSONResponse(vm)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.head("/")
def head_root() -> JSONResponse:
    return JSONResponse({})


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", __PORT_LIT__))
    uvicorn.run(app, host="0.0.0.0", port=port)
'''


def render_fastapi_server(
    report_path: str | Path, port: int, layout: str, title: str, api_path: str = "/api/data"
) -> str:
    return (
        _SERVER_MAIN.replace("__REPORT_LIT__", json.dumps(str(report_path)))
        .replace("__LAYOUT_LIT__", json.dumps(layout or ""))
        .replace("__TITLE_LIT__", json.dumps(title or ""))
        .replace("__API_LIT__", json.dumps(api_path))
        .replace("__PORT_LIT__", json.dumps(str(int(port))))
    )


def render_requirements() -> str:
    return "fastapi>=0.110\nuvicorn[standard]>=0.29\n"


def render_start_sh(port: int) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "# Idempotent launcher: frees the port, then serves. Safe to re-run.\n"
        "set -euo pipefail\n"
        f'PORT="${{PORT:-{int(port)}}}"\n'
        'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        'if command -v fuser >/dev/null 2>&1; then fuser -k "${PORT}/tcp" 2>/dev/null || true; fi\n'
        'cd "$HERE"\n'
        'exec python3 -m uvicorn server.main:app --host 0.0.0.0 --port "$PORT"\n'
    )


def _write_serve_readme(out: Path, manifest: dict) -> None:
    lines = [
        f"# {manifest['title']}",
        "",
        f"- Tier: `{manifest['tier']}`",
        f"- Layout: `{manifest['layout']}`",
        f"- Records: {manifest['record_count']}",
        "",
        "## Serve",
        "",
        "```bash",
        manifest["serve_command"],
        "```",
        "",
    ]
    if manifest["tier"] == "static":
        lines += [
            "This page is self-contained (no external scripts) and passes the CSP gate,",
            "so it can also be published directly via the claude.ai Artifact tool.",
            "",
        ]
    else:
        lines += [
            "Install deps first: `pip install -r server/requirements.txt`. The page polls",
            f"`{manifest['api_path']}` every {manifest['poll_seconds']}s; the server re-reads the",
            "report JSON on each request. Override the report path with $DASHBOARD_REPORT.",
            "",
        ]
    (out / "SERVE.md").write_text("\n".join(lines), encoding="utf-8")


# ── Builders (each runs the CSP gate before returning) ───────────────────────
def build_static(
    report_path: str | Path,
    output_dir: str | Path,
    layout: str | None = None,
    title: str | None = None,
    source_report: str | None = None,
) -> dict:
    data = load_report(report_path)
    vm = build_view_model(
        data, layout=layout, title=title, source_report=source_report or Path(report_path).name
    )
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    index = out / "index.html"
    index.write_text(render_static_html(vm), encoding="utf-8")
    assert_csp_safe([index])  # pre-serve gate -- raises CspViolationError on failure
    manifest = {
        "tier": "static",
        "layout": vm["layout"],
        "title": vm["title"],
        "output_dir": str(out),
        "index_html": str(index),
        "record_count": vm["record_count"],
        "serve_command": f"python3 -m http.server {DEFAULT_PORT} --bind 0.0.0.0 --directory {out}",
        "artifact_ready": True,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _write_serve_readme(out, manifest)
    return manifest


def build_fastapi(
    report_path: str | Path,
    output_dir: str | Path,
    port: int = DEFAULT_PORT,
    poll_seconds: int = 30,
    layout: str | None = None,
    title: str | None = None,
    source_report: str | None = None,
    api_path: str = "/api/data",
) -> dict:
    data = load_report(report_path)
    vm = build_view_model(
        data, layout=layout, title=title, source_report=source_report or Path(report_path).name
    )
    out = Path(output_dir)
    (out / "server").mkdir(parents=True, exist_ok=True)
    (out / "static").mkdir(parents=True, exist_ok=True)

    index = out / "static" / "index.html"
    index.write_text(render_fastapi_index_html(vm, poll_seconds, api_path), encoding="utf-8")
    assert_csp_safe([index])  # pre-serve gate -- raises CspViolationError on failure

    (out / "server" / "main.py").write_text(
        render_fastapi_server(report_path, port, vm["layout"], vm["title"], api_path),
        encoding="utf-8",
    )
    (out / "server" / "requirements.txt").write_text(render_requirements(), encoding="utf-8")
    start = out / "start.sh"
    start.write_text(render_start_sh(port), encoding="utf-8")
    os.chmod(start, 0o755)

    # Copy the pure-python builder + CSP gate so the server can import them.
    here = Path(__file__).resolve().parent
    for mod in ("build_dashboard.py", "csp_check.py"):
        shutil.copyfile(here / mod, out / "server" / mod)

    manifest = {
        "tier": "fastapi",
        "layout": vm["layout"],
        "title": vm["title"],
        "output_dir": str(out),
        "index_html": str(index),
        "server_main": str(out / "server" / "main.py"),
        "record_count": vm["record_count"],
        "port": int(port),
        "api_path": api_path,
        "poll_seconds": int(poll_seconds),
        "serve_command": f"PORT={int(port)} bash {start}",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _write_serve_readme(out, manifest)
    return manifest


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build a dashboard from a skill's reports/*.json output."
    )
    ap.add_argument("--report", required=True, help="Path to the source report JSON")
    ap.add_argument("--tier", choices=["static", "fastapi", "spa"], default="static")
    ap.add_argument(
        "--output-dir", default=None, help="Output directory (default reports/dashboard/<tier>)"
    )
    ap.add_argument(
        "--layout", choices=["watchlist", "breadth", "portfolio", "generic"], default=None
    )
    ap.add_argument("--title", default=None)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--poll-seconds", type=int, default=30)
    args = ap.parse_args(argv)

    if args.tier == "spa":
        print(
            "SPA tier is not scaffolded by this script. Escalate to a Vite/React SPA "
            "manually only when you need client-side routing or multi-page interactivity. "
            "See references/serve_tiers.md.",
            file=sys.stderr,
        )
        return 2

    report = Path(args.report)
    if not report.exists():
        print(f"Report not found: {report}", file=sys.stderr)
        return 1
    if args.port not in PORT_RANGE:
        print(
            f"Warning: port {args.port} is outside the {PORT_RANGE.start}-{PORT_RANGE.stop - 1} convention.",
            file=sys.stderr,
        )

    out = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / args.tier
    try:
        if args.tier == "static":
            manifest = build_static(report, out, layout=args.layout, title=args.title)
        else:
            manifest = build_fastapi(
                report,
                out,
                port=args.port,
                poll_seconds=args.poll_seconds,
                layout=args.layout,
                title=args.title,
            )
    except CspViolationError as exc:
        print("BUILD FAILED — CSP gate rejected the generated page:", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 3

    print(json.dumps(manifest, indent=2))
    print(f"\nServe: {manifest['serve_command']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
