# Serve Tiers & Decision Rule

This skill renders another skill's `reports/*.json` as a dashboard. **Check the
Artifact path first** — most requests are snapshots and need no local server at
all.

## Step 0 — Does the Artifact tool already cover this?

The claude.ai **Artifact** tool publishes a self-contained HTML page to a hosted,
default-private URL. For a **snapshot** view of a report — a watchlist, a breadth
reading, a portfolio table as of the last run — that is the simplest, most
shareable deliverable, and it requires no local process.

Use an Artifact when:

- The data is a **point-in-time snapshot** (no intraday refresh needed).
- The user wants a **link they can keep or share**, or a printable page.
- All content fits in one self-contained file (Artifacts block external CDNs,
  fonts, and `fetch`/XHR to any other host — everything must be inlined).

`build_dashboard.py --tier static` produces exactly this: one self-contained
`index.html` with the view model inlined, **no external scripts**, CSP-clean.
Open it locally, serve it, or hand its body to the Artifact tool to publish.

Only build a **local serve stack (Tier 2/3)** when a live-refreshing intraday
monitor is genuinely required — the Artifact CSP cannot poll a live backend, so
auto-refreshing data needs a server the browser can reach on localhost.

## The three tiers

| Tier | Use when | Stack | Serve |
|------|----------|-------|-------|
| **1 — static** | Snapshot; publishable as an Artifact | One self-contained HTML file, inline CSS/JS, no CDN | Artifact tool, or `python3 -m http.server` |
| **2 — fastapi** | Data refreshes intraday; page must poll a live backend | FastAPI `/api/data` (re-reads the report JSON) + polling HTML | `bash start.sh` |
| **3 — spa** | Client-side routing, multi-page, or React-level component interactivity | FastAPI backend + Vite/React frontend | manual scaffold |

**Decision rule:** Start at Tier 1 (Artifact/static). Escalate to Tier 2 only
when the data genuinely refreshes while the user watches and a snapshot will not
do. Escalate to Tier 3 only when Tier 2's single polling page cannot express the
UI (routing / multiple pages / heavy component state). Tier 3 is **not** scaffolded
by `build_dashboard.py` — build it by hand when the need is real; do not reach for
it by default.

## Fixed port convention

- **Default port: 8770.** Range **8770–8779** is reserved for this skill's
  dashboards; use higher numbers in the range when running several at once.
- The FastAPI server binds `0.0.0.0` (not `127.0.0.1`/`localhost`) so preview
  proxies can reach it.
- `start.sh` is **idempotent**: it frees the port (`fuser -k`) before launching,
  so re-running it after a restart just works.
- Override at serve time with the `PORT` env var; override the report path with
  `DASHBOARD_REPORT`.

## Tier 2 bundle layout (`--tier fastapi`)

```
<output>/
├── server/
│   ├── main.py            # FastAPI app: /api/data (re-reads JSON), /healthz, HEAD /, StaticFiles mount
│   ├── build_dashboard.py # copied so the server can rebuild the view model
│   ├── csp_check.py       # copied dependency of build_dashboard
│   └── requirements.txt   # fastapi + uvicorn (install before serving)
├── static/
│   └── index.html         # polling page: fetch('/api/data') on an interval
├── start.sh               # idempotent launcher (binds 0.0.0.0)
├── manifest.json
└── SERVE.md
```

Install and serve:

```bash
pip install -r <output>/server/requirements.txt
PORT=8770 bash <output>/start.sh
```

`fastapi` and `uvicorn` are runtime-only for this tier and live in the generated
`requirements.txt` — they are not dependencies of the skill's own build/test code,
which is pure stdlib and runs offline.

## Verifying before you serve

1. **CSP gate (always):** `python3 scripts/csp_check.py <output>` — must report no
   violations. `build_dashboard.py` runs this automatically and fails the build on
   any violation.
2. **Live check (Tier 2):** after `start.sh`, confirm the port answers:
   ```bash
   for i in $(seq 1 15); do curl -sf http://127.0.0.1:8770/healthz >/dev/null && echo ok && break || sleep 1; done
   ```
