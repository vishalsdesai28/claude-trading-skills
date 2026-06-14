"""FMP ``/api/v3`` → ``/stable`` URL compatibility shim.

FMP retired the legacy ``/api/v3/`` surface on 2025-08-31; API keys issued
after that date receive ``403 "Legacy Endpoint"`` on every ``/api/v3/`` request.

This helper rewrites a legacy v3-style URL (and its params) to the ``/stable``
equivalent. It is applied ONLY at construction points that build hardcoded v3
URLs and are *not* part of an explicit stable→v3 fallback list. Methods that
already iterate a ``_FMP_ENDPOINTS`` stable→v3 table must NOT route through this
shim, or the v3 fallback entry would be rewritten back to stable and the
fallback contract would break.

Note on endpoint naming: ``/stable`` accepts both underscore (legacy-style) and
hyphen (modern) names, but for several endpoints the underscore variant is free
while the hyphen variant is paid-gated (e.g. ``economic_calendar`` vs
``economic-calendar``, ``stock_news`` vs ``news/stock``). The rewriter preserves
underscore names to keep free-tier access working.
"""

from __future__ import annotations

from datetime import date, timedelta

_STABLE = "https://financialmodelingprep.com/stable"

# v3 path segment (symbol carried in the path) → /stable path (symbol via ?symbol=)
_PATH_WITH_SYMBOL = {
    "quote": "/quote",
    "profile": "/profile",
    "income-statement": "/income-statement",
    "balance-sheet-statement": "/balance-sheet-statement",
    "cash-flow-statement": "/cash-flow-statement",
    "key-metrics": "/key-metrics",
    "ratios": "/ratios",
    "enterprise-values": "/enterprise-values",
    "market-capitalization": "/market-capitalization",
    "institutional-holder": "/institutional-ownership/symbol-ownership",
    "etf-holder": "/etf-holdings",
    "rating": "/rating",
    "discounted-cash-flow": "/discounted-cash-flow",
}

# v3 path → /stable path for endpoints that carry NO path symbol and whose
# /stable name differs from the v3 name. Explicit because the underscore
# (v3-style) /stable name 404s for these; the hyphenated name is the live one
# (verified 2026-06: /stable/sp500_constituent and /stable/earning_calendar
# both 404, the hyphenated variants both 200). These override the
# underscore-preserving best-effort fallthrough below.
_PATH_RENAME_NO_SYMBOL = {
    "sp500_constituent": "/sp500-constituent",
    "earning_calendar": "/earnings-calendar",
}


def v3_to_stable(url: str, params: dict | None = None) -> tuple[str, dict]:
    """Rewrite a legacy FMP v3 URL to its ``/stable`` equivalent.

    No-op for URLs that do not contain ``/api/v3/``. Unmapped endpoints fall
    back to a 1:1 path swap, intentionally preserving underscore naming.
    """
    params = {} if params is None else dict(params)

    if "/api/v3/" not in url:
        return url, params

    after = url.split("/api/v3/", 1)[1].rstrip("/")

    # historical-price-full has a dividend sub-path and a price variant
    if after.startswith("historical-price-full/stock_dividend/"):
        params["symbol"] = after[len("historical-price-full/stock_dividend/") :]
        return _STABLE + "/dividends", params
    if after.startswith("historical-price-full/"):
        params["symbol"] = after[len("historical-price-full/") :]
        # The stable EOD endpoint ignores ``timeseries``; convert to a from/to
        # range (2x calendar days covers N trading days with weekend headroom).
        timeseries = params.pop("timeseries", None)
        if timeseries:
            today = date.today()
            params.setdefault("from", (today - timedelta(days=int(timeseries) * 2)).isoformat())
            params.setdefault("to", today.isoformat())
        return _STABLE + "/historical-price-eod/full", params

    # historical/earning_calendar/{symbol} → earnings?symbol=
    if after.startswith("historical/earning_calendar/"):
        params["symbol"] = after[len("historical/earning_calendar/") :]
        return _STABLE + "/earnings", params

    # symbol-in-path endpoints → ?symbol=
    for v3_path, stable_path in _PATH_WITH_SYMBOL.items():
        if after.startswith(v3_path + "/"):
            params["symbol"] = after[len(v3_path) + 1 :]
            return _STABLE + stable_path, params
        if after == v3_path:
            return _STABLE + stable_path, params

    # Explicit hyphenated renames for symbol-less endpoints whose underscore
    # /stable form 404s (must come before the underscore-preserving fallthrough).
    if after in _PATH_RENAME_NO_SYMBOL:
        return _STABLE + _PATH_RENAME_NO_SYMBOL[after], params

    # Best-effort 1:1 swap; preserve underscore names (free-tier friendly).
    return _STABLE + "/" + after, params
