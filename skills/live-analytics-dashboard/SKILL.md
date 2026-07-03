---
name: live-analytics-dashboard
description: "Render another skill's reports/*.json (watchlist, breadth, or portfolio monitors) as a dashboard. Use when the user wants to visualize a screener/monitor result as a web page or a live-refreshing intraday monitor. Prefer the built-in Artifact tool for snapshots; escalate to a local FastAPI + polling serve stack (fixed port 8770) only when data must refresh live. Every generated page is CSP-safety gated (no inline handlers, addEventListener only) before serving."
---

# Live Analytics Dashboard

Turn a skill's JSON output into a dashboard. Detect the report shape
(watchlist / breadth / portfolio / generic), build a KPI + sortable-table view,
and either publish it as a snapshot Artifact or serve it as a live-refreshing
page. Enforce CSP-safety on every generated page.

## When to Use

- The user wants to **see** a screener / monitor result as a web page: "show me
  the watchlist as a dashboard", "chart the breadth reading", "a portfolio
  monitor from my positions JSON".
- A report needs a **live-refreshing** intraday view (a parabolic-short
  watchlist, a portfolio P&L monitor) that polls fresh numbers.

Do NOT use for: a static chart image (use matplotlib/plotly `savefig`), a plain
markdown report, or a one-line answer.

## Step 0 (do this first): can the Artifact tool cover it?

For a **snapshot** — a point-in-time view the user keeps or shares — the
built-in **Artifact** tool is the right delivery: it publishes one
self-contained HTML page to a hosted URL, no local process. This skill's
`--tier static` output is built exactly for that: one file, all CSS/JS inline,
no external scripts, CSP-clean. Publish it via the Artifact tool, or open/serve
it locally.

Only build the **local FastAPI serve stack** (Tier 2) when a live-refreshing
intraday monitor is genuinely required — the Artifact CSP blocks `fetch`/XHR to
external hosts, so auto-refreshing data needs a server the browser can reach on
localhost. See `references/serve_tiers.md` for the full decision rule.

## Serve tiers (fixed port convention)

| Tier | Use when | Build | Serve |
|------|----------|-------|-------|
| **1 — static** | Snapshot; Artifact-publishable | `--tier static` | Artifact tool, or `python3 -m http.server 8770` |
| **2 — fastapi** | Live intraday refresh | `--tier fastapi` | `bash start.sh` (binds 0.0.0.0) |
| **3 — spa** | Routing / multi-page / heavy React | not scaffolded here | build by hand |

Default port **8770**; range **8770–8779** reserved. Tier 3 is intentionally not
scaffolded — escalate manually only when Tier 2 cannot express the UI.

## Workflow

1. **Locate the source report.** Take a skill's `reports/*.json` (watchlist,
   breadth, portfolio, or any record set). Confirm it exists.
2. **Pick the tier.** Snapshot -> Tier 1 (prefer the Artifact tool). Live
   intraday refresh -> Tier 2. Consult `references/serve_tiers.md` when unsure.
3. **Build.** Run `scripts/build_dashboard.py` for the chosen tier. Layout is
   auto-detected; override with `--layout` and set a heading with `--title`.
4. **The CSP gate runs automatically.** `build_dashboard.py` scans every
   generated page with `scripts/csp_check.py` and **fails the build** on any
   inline handler, `eval`, `new Function`, `javascript:` URL, or string-form
   timer. Fix the template and rebuild if it fails.
5. **Deliver.**
   - Tier 1: publish the self-contained `index.html` via the **Artifact tool**,
     or serve it with the printed `python3 -m http.server` command.
   - Tier 2: `pip install -r <output>/server/requirements.txt`, then
     `PORT=8770 bash <output>/start.sh`; the page polls `/api/data` and the
     server re-reads the report JSON on each request. Verify `/healthz` responds.

## Consuming other skills' output

Accept the DOCUMENTED JSON output shape of an upstream skill as input — do not
import sibling-skill modules. The builder auto-detects common shapes:

- **watchlist** — a `candidates` / `watchlist` array of ticker records
  (parabolic-short-trade-planner, VCP, PEAD, CANSLIM, edge screeners).
- **portfolio** — a `positions` / `holdings` array (portfolio-manager,
  trader-memory-core).
- **breadth** — top-level advancer/decliner/new-high/McClellan style metrics
  (market-breadth-analyzer, uptrend-analyzer).
- **generic** — any dict with a record array plus scalar summary fields.

Top-level scalars become KPI cards; the primary array becomes a sortable table.
Percentage-named fields are color-coded (green/red) and currency/percent columns
are formatted client-side so sorting stays numeric.

## Scripts

**`build_dashboard.py`** — build a dashboard from a report. Pure stdlib +
`csp_check`; no network, no FastAPI import at build time.

```bash
# Tier 1 — self-contained snapshot (Artifact-ready)
python3 skills/live-analytics-dashboard/scripts/build_dashboard.py \
  --report reports/parabolic_short_2026-07-03.json \
  --tier static --output-dir reports/dashboard/static

# Tier 2 — live-refreshing FastAPI monitor on the convention port
python3 skills/live-analytics-dashboard/scripts/build_dashboard.py \
  --report reports/portfolio_snapshot.json \
  --tier fastapi --port 8770 --poll-seconds 30 \
  --output-dir reports/dashboard/live

# Override layout detection / heading
python3 skills/live-analytics-dashboard/scripts/build_dashboard.py \
  --report reports/breadth.json --tier static --layout breadth --title "Breadth"
```

**`csp_check.py`** — standalone CSP gate (also invoked by the builder):

```bash
python3 skills/live-analytics-dashboard/scripts/csp_check.py reports/dashboard/static
# exit 0 = clean, exit 1 = violations (printed with file:line and category)
```

## Output

Written to the `--output-dir` (default `reports/dashboard/<tier>/`):

- **Tier 1:** `index.html` (self-contained), `manifest.json`, `SERVE.md`.
- **Tier 2:** `static/index.html`, `server/main.py` (+ copied `build_dashboard.py`,
  `csp_check.py`, `requirements.txt`), idempotent `start.sh`, `manifest.json`,
  `SERVE.md`.

`manifest.json` includes the tier, detected layout, record count, and the exact
serve command.

## Resources

- `references/serve_tiers.md` — Artifact-first decision rule, three tiers, port
  convention, Tier-2 bundle layout, and verification steps.
- `references/csp_safety.md` — the CSP-safety checklist and the safe replacement
  for every blocked pattern. Read before hand-editing any template.
- `references/chart_snippets.md` — CSP-safe visual widgets (Artifact-safe CSS
  bars / inline SVG; CDN charts for local-served Tier 2 only).
