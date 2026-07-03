# CSP Safety Checklist

Any HTML this skill serves runs inside a Content-Security-Policy sandbox — the
claude.ai Artifact host, embedded iframes, and most preview surfaces. Under CSP,
the unsafe patterns below are **silently blocked**: no error banner, just a dead
button or a blank page. Always use the safe alternative, and let the
`csp_check` gate verify the output before serving.

The gate (`scripts/csp_check.py`) is a grep-style scanner. `build_dashboard.py`
runs `assert_csp_safe([...])` on every generated page and **fails the build** if
any pattern below is present. Run it standalone too:

```bash
python3 scripts/csp_check.py path/to/dir_or_file.html   # exit 1 on any violation
```

## Blocked patterns and their safe replacements

| Blocked (CSP kills it silently) | Safe alternative | Gate category |
|---|---|---|
| `<button onclick="fn()">` | `el.addEventListener('click', fn)` | `inline-handler` |
| `<div onmouseover="fn()">` | `el.addEventListener('mouseover', fn)` | `inline-handler` |
| Any `on*="…"` HTML attribute | `el.addEventListener(event, fn)` | `inline-handler` |
| `innerHTML` containing an `on*=` handler | `document.createElement()` + `addEventListener` | `inline-handler` |
| `eval("code")` | Direct function calls | `eval` |
| `new Function("code")` | Named function declarations | `new-function` |
| `setTimeout("code", ms)` / `setInterval("code", ms)` | `setTimeout(fn, ms)` (function reference) | `string-timer` |
| `<a href="javascript:…">` | `<a href="#" data-action="…">` + `addEventListener` | `javascript-url` |

**Allowed** (the gate does not flag these): DOM-property assignment to a
function reference — `el.onclick = fn` — is CSP-safe (it is a property, not an
HTML attribute). `addEventListener('click', fn)` is always preferred and is the
only form used by the generated templates.

## Rules for hand-written or edited templates

1. **Events via `addEventListener` only.** Never write `on*="…"` attributes. Bind
   listeners inside a `DOMContentLoaded` handler after the elements exist.
2. **Build DOM with `createElement` + `textContent`.** Prefer `textContent` over
   `innerHTML`; if you must use `innerHTML`, it must contain no `on*=` handlers.
   `textContent` also avoids injection from report string fields.
3. **No `eval`, no `new Function`, no string-argument timers.** Pass a function
   reference to `setTimeout`/`setInterval`, never a code string.
4. **No `javascript:` URLs.** Use `href="#"` + a `data-*` attribute read by an
   `addEventListener` handler.

## Template-literal hygiene

When building HTML strings inside JS template literals, a CSS semicolon inside a
`${}` expression terminates the expression early and silently corrupts the markup:

```javascript
// BAD — the semicolon ends the ${} expression, then `font-weight:600}` leaks out
const el = `<div style="color:${positive ? 'green' : 'red'; font-weight:600}">`;

// GOOD — close the ${} first, then continue the attribute string
const el = `<div style="color:${positive ? 'green' : 'red'};font-weight:600">`;
```

Rule: **never put a CSS semicolon inside `${}`** — always close the `}` before the
semicolon.

## Embedding report JSON safely

The static tier inlines the view model as `const DATA = {…};`. Embed it with
`json.dumps(...)` and escape `<`, `>`, `&`, and the U+2028/U+2029 line separators
so a string field can never break out of the `<script>` context (`build_dashboard._embed_json`
does this). This escaping also neutralizes an `onclick="…"` that happens to
appear inside a data string — `json.dumps` renders the quote as `\"`, which the
inline-handler pattern will not match. The gate still scans the whole served
file, so a genuine `javascript:` URL or inline handler in the markup fails the
build as intended.
