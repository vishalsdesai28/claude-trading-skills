"""Pre-trade liquidity / execution-cost check for long stock trades.

Computes liquidity and tradability metrics from daily OHLCV bars and a
current quote:

- Bid-ask spread (absolute, %, bps)
- Average daily volume (ADV) and average daily dollar volume (ADDV)
- Turnover vs. float / shares outstanding, days-to-trade-float
- Amihud illiquidity ratio: mean(|ret| / daily_dollar_volume) * 1e9
- Volume coefficient of variation (std / mean)
- Estimated market-impact slippage for a given order size via the
  square-root model: impact_bps = sigma * sqrt(order / ADV) * 1e4

Graded thresholds and multi-ticker comparison are included. Data comes
from yfinance (keyless) or FMP daily history; network libraries are
imported lazily inside the fetch functions so the pure metric functions
import with the standard library only and tests run fully offline.

The JSON output shape is consumed by ``position_sizer.py --liquidity-json``
to apply a ``--max-slippage-bps`` gate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from datetime import datetime

SCHEMA_VERSION = "1.0"

TRADING_DAYS_PER_YEAR = 252

# Slippage grade thresholds in basis points (upper bound inclusive).
SLIPPAGE_GRADES = [
    (10.0, "minimal"),
    (25.0, "low"),
    (50.0, "moderate"),
    (100.0, "high"),
]


# ─── Pure metric functions (stdlib only) ─────────────────────────────────────


def compute_spread(bid: float | None, ask: float | None) -> dict:
    """Return spread metrics from a bid/ask quote.

    absolute = ask - bid, relative % vs. midpoint, and basis points.
    Returns all-None when bid/ask are missing or non-positive.
    """
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return {"spread": None, "spread_pct": None, "spread_bps": None}
    midpoint = (bid + ask) / 2
    spread = ask - bid
    return {
        "spread": round(spread, 4),
        "spread_pct": round(spread / midpoint * 100, 4),
        "spread_bps": round(spread / midpoint * 10000, 2),
    }


def compute_amihud(closes: list[float], volumes: list[float]) -> float | None:
    """Amihud (2002) illiquidity ratio, scaled by 1e9.

    mean( |daily return| / daily dollar volume ) * 1e9 over the sample,
    skipping days with zero dollar volume. Higher = less liquid.
    """
    if len(closes) < 2:
        return None
    ratios: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        ret = abs(closes[i] / prev - 1.0)
        dollar_vol = closes[i] * volumes[i]
        if dollar_vol > 0:
            ratios.append(ret / dollar_vol)
    if not ratios:
        return None
    return statistics.mean(ratios) * 1e9


def daily_returns(closes: list[float]) -> list[float]:
    """Simple daily returns from a close series."""
    out: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev > 0:
            out.append(closes[i] / prev - 1.0)
    return out


def daily_volatility(closes: list[float]) -> float | None:
    """Sample standard deviation of daily returns (decimal fraction)."""
    rets = daily_returns(closes)
    if len(rets) < 2:
        return None
    return statistics.stdev(rets)


def volume_cv(volumes: list[float]) -> float | None:
    """Coefficient of variation of daily volume (std / mean)."""
    if len(volumes) < 2:
        return None
    mean_v = statistics.mean(volumes)
    if mean_v <= 0:
        return None
    return statistics.stdev(volumes) / mean_v


def compute_turnover(
    adv: float, float_shares: float | None, shares_outstanding: float | None
) -> dict:
    """Turnover vs. float (preferred) or shares outstanding.

    Returns daily turnover ratio, annualized turnover %, days-to-trade-float,
    and which base was used.
    """
    base = float_shares or shares_outstanding
    if not base or base <= 0 or adv <= 0:
        return {
            "turnover_ratio_daily": None,
            "turnover_annualized_pct": None,
            "days_to_trade_float": None,
            "turnover_base": None,
        }
    daily = adv / base
    return {
        "turnover_ratio_daily": round(daily, 6),
        "turnover_annualized_pct": round(daily * TRADING_DAYS_PER_YEAR * 100, 1),
        "days_to_trade_float": round(base / adv, 1),
        "turnover_base": "float" if float_shares else "shares_outstanding",
    }


def estimate_slippage_bps(order_shares: float, adv: float, sigma: float) -> float:
    """Square-root market-impact model.

    impact_bps = sigma * sqrt(order_shares / ADV) * 1e4

    where sigma is daily return volatility (decimal). Returns 0.0 when any
    input is non-positive.
    """
    if adv <= 0 or order_shares <= 0 or sigma <= 0:
        return 0.0
    return sigma * math.sqrt(order_shares / adv) * 1e4


def max_shares_under_slippage(adv: float, sigma: float, max_slippage_bps: float) -> int | None:
    """Largest order (shares) whose square-root impact stays within budget.

    Inverts ``estimate_slippage_bps``:
        Q_max = ADV * (budget / (sigma * 1e4)) ** 2
    Returns None when the model cannot be evaluated.
    """
    if adv <= 0 or sigma <= 0 or max_slippage_bps <= 0:
        return None
    ratio = max_slippage_bps / (sigma * 1e4)
    return int(adv * ratio * ratio)


def grade_slippage(impact_bps: float | None) -> str | None:
    """Grade an impact estimate: minimal / low / moderate / high / severe."""
    if impact_bps is None:
        return None
    for upper, label in SLIPPAGE_GRADES:
        if impact_bps <= upper:
            return label
    return "severe"


def grade_liquidity(
    avg_dollar_volume: float | None,
    spread_pct: float | None,
    amihud: float | None,
) -> str:
    """Overall liquidity grade for US equities.

    Primary driver is average daily dollar volume; a very high Amihud ratio
    downgrades the grade one notch to reflect price-impact sensitivity.
    """
    if avg_dollar_volume is None:
        return "unknown"
    if avg_dollar_volume > 500_000_000:
        grade = "very_high"
    elif avg_dollar_volume > 50_000_000:
        grade = "high"
    elif avg_dollar_volume > 5_000_000:
        grade = "moderate"
    elif avg_dollar_volume > 500_000:
        grade = "low"
    else:
        grade = "very_low"

    order = ["very_low", "low", "moderate", "high", "very_high"]
    # Downgrade one notch on an illiquid Amihud reading.
    if amihud is not None and amihud > 1.0 and grade != "very_low":
        grade = order[max(0, order.index(grade) - 1)]
    # Downgrade one notch on a very wide spread.
    if spread_pct is not None and spread_pct > 1.0 and grade != "very_low":
        grade = order[max(0, order.index(grade) - 1)]
    return grade


# ─── Per-ticker assembly ─────────────────────────────────────────────────────


def analyze_ticker(
    ticker: str,
    bars: list[dict],
    quote: dict | None = None,
    order_shares: float | None = None,
    order_dollars: float | None = None,
) -> dict:
    """Compute the full liquidity metric set for one ticker.

    ``bars`` is a list of ``{"close": float, "volume": float}`` daily bars.
    ``quote`` optionally carries bid/ask/current_price/shares_outstanding/
    float_shares. Order size may be given in shares or dollars.
    """
    quote = quote or {}
    closes = [float(b["close"]) for b in bars]
    volumes = [float(b["volume"]) for b in bars]

    if not closes:
        return {"ticker": ticker, "error": "no price bars available"}

    current_price = quote.get("current_price") or closes[-1]
    adv = statistics.mean(volumes) if volumes else 0.0
    median_volume = statistics.median(volumes) if volumes else 0.0
    dollar_vols = [c * v for c, v in zip(closes, volumes)]
    addv = statistics.mean(dollar_vols) if dollar_vols else 0.0
    sigma = daily_volatility(closes)
    amihud = compute_amihud(closes, volumes)
    cv = volume_cv(volumes)

    spread = compute_spread(quote.get("bid"), quote.get("ask"))
    turnover = compute_turnover(adv, quote.get("float_shares"), quote.get("shares_outstanding"))

    # Resolve order size.
    resolved_shares: float | None = order_shares
    if resolved_shares is None and order_dollars and current_price > 0:
        resolved_shares = order_dollars / current_price

    slippage_bps: float | None = None
    pct_of_adv: float | None = None
    if resolved_shares and sigma is not None and adv > 0:
        slippage_bps = round(estimate_slippage_bps(resolved_shares, adv, sigma), 2)
        pct_of_adv = round(resolved_shares / adv * 100, 2)

    grade = grade_liquidity(addv, spread["spread_pct"], amihud)

    warnings: list[str] = []
    if addv < 1_000_000:
        warnings.append("micro-cap: average dollar volume < $1M/day")
    if spread["spread_pct"] is not None and spread["spread_pct"] > 2.0:
        warnings.append("wide spread: > 2%")
    if cv is not None and cv > 1.0:
        warnings.append("spiky volume: coefficient of variation > 1.0")
    if slippage_bps is not None and slippage_bps > 50:
        warnings.append("high market impact: estimated slippage > 50 bps")

    return {
        "ticker": ticker,
        "observations": len(closes),
        "current_price": round(current_price, 4),
        "bid": quote.get("bid"),
        "ask": quote.get("ask"),
        "spread": spread["spread"],
        "spread_pct": spread["spread_pct"],
        "spread_bps": spread["spread_bps"],
        "avg_daily_volume": int(adv),
        "median_daily_volume": int(median_volume),
        "avg_dollar_volume": round(addv, 0),
        "daily_volatility": round(sigma, 6) if sigma is not None else None,
        "daily_volatility_pct": round(sigma * 100, 2) if sigma is not None else None,
        "volume_cv": round(cv, 3) if cv is not None else None,
        "shares_outstanding": quote.get("shares_outstanding"),
        "float_shares": quote.get("float_shares"),
        "turnover_ratio_daily": turnover["turnover_ratio_daily"],
        "turnover_annualized_pct": turnover["turnover_annualized_pct"],
        "days_to_trade_float": turnover["days_to_trade_float"],
        "turnover_base": turnover["turnover_base"],
        "amihud_illiquidity": round(amihud, 4) if amihud is not None else None,
        "order_shares": int(resolved_shares) if resolved_shares else None,
        "pct_of_adv": pct_of_adv,
        "estimated_slippage_bps": slippage_bps,
        "slippage_grade": grade_slippage(slippage_bps),
        "liquidity_grade": grade,
        "warnings": warnings,
    }


def compare_tickers(results: list[dict]) -> dict | None:
    """Rank tickers by average dollar volume for a comparison summary."""
    valid = [r for r in results if "error" not in r and r.get("avg_dollar_volume")]
    if len(valid) < 2:
        return None
    ranked = sorted(valid, key=lambda r: r["avg_dollar_volume"], reverse=True)
    return {
        "ranked_by_dollar_volume": [r["ticker"] for r in ranked],
        "most_liquid": ranked[0]["ticker"],
        "least_liquid": ranked[-1]["ticker"],
    }


def build_report(
    results: list[dict],
    order_shares: float | None,
    order_dollars: float | None,
) -> dict:
    """Assemble the full JSON report from per-ticker analyses."""
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "order": {"shares": order_shares, "dollars": order_dollars},
        "tickers": results,
    }
    comparison = compare_tickers(results)
    if comparison:
        report["comparison"] = comparison
    return report


def generate_markdown_report(report: dict) -> str:
    """Render a markdown summary of the liquidity report."""
    lines = [
        "# Pre-Trade Liquidity Check",
        "**Generated:** {}".format(report["generated"]),
    ]
    order = report.get("order", {})
    if order.get("shares"):
        lines.append("**Order size:** {} shares".format(int(order["shares"])))
    elif order.get("dollars"):
        lines.append("**Order size:** ${:,.0f}".format(order["dollars"]))
    lines.append("")

    for r in report["tickers"]:
        lines.append("## {}".format(r["ticker"]))
        if "error" in r:
            lines.append("- ERROR: {}".format(r["error"]))
            lines.append("")
            continue
        lines.append("- Liquidity grade: **{}**".format(r["liquidity_grade"]))
        lines.append("- Current price: ${}".format(r["current_price"]))
        if r["spread_bps"] is not None:
            lines.append("- Bid-ask spread: {} bps ({}%)".format(r["spread_bps"], r["spread_pct"]))
        lines.append("- Avg daily volume: {:,} shares".format(r["avg_daily_volume"]))
        lines.append("- Avg dollar volume: ${:,.0f}".format(r["avg_dollar_volume"]))
        if r["daily_volatility_pct"] is not None:
            lines.append("- Daily volatility: {}%".format(r["daily_volatility_pct"]))
        if r["volume_cv"] is not None:
            lines.append("- Volume CV: {}".format(r["volume_cv"]))
        if r["turnover_annualized_pct"] is not None:
            lines.append(
                "- Turnover (annualized, {}): {}%".format(
                    r["turnover_base"], r["turnover_annualized_pct"]
                )
            )
        if r["days_to_trade_float"] is not None:
            lines.append("- Days to trade float: {}".format(r["days_to_trade_float"]))
        if r["amihud_illiquidity"] is not None:
            lines.append("- Amihud illiquidity (x1e9): {}".format(r["amihud_illiquidity"]))
        if r["estimated_slippage_bps"] is not None:
            lines.append(
                "- Estimated slippage: {} bps ({}) at {}% of ADV".format(
                    r["estimated_slippage_bps"], r["slippage_grade"], r["pct_of_adv"]
                )
            )
        for w in r["warnings"]:
            lines.append(f"- WARNING: {w}")
        lines.append("")

    if "comparison" in report:
        c = report["comparison"]
        lines.append("## Comparison")
        lines.append(
            "- Ranked by dollar volume: {}".format(", ".join(c["ranked_by_dollar_volume"]))
        )
        lines.append("- Most liquid: **{}**".format(c["most_liquid"]))
        lines.append("- Least liquid: **{}**".format(c["least_liquid"]))
        lines.append("")

    return "\n".join(lines) + "\n"


# ─── Data fetchers (network libs imported lazily) ────────────────────────────


def fetch_yfinance(ticker: str, period: str = "3mo") -> tuple[list[dict], dict]:
    """Fetch daily bars + quote from Yahoo Finance (keyless)."""
    import yfinance as yf  # lazy

    t = yf.Ticker(ticker)
    info = t.info or {}
    hist = t.history(period=period, auto_adjust=False)
    bars: list[dict] = []
    if hist is not None and not hist.empty:
        for close, vol in zip(hist["Close"].tolist(), hist["Volume"].tolist()):
            bars.append({"close": float(close), "volume": float(vol)})
    quote = {
        "bid": info.get("bid"),
        "ask": info.get("ask"),
        "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "shares_outstanding": info.get("sharesOutstanding"),
        "float_shares": info.get("floatShares"),
    }
    return bars, quote


def fetch_fmp(ticker: str, api_key: str, days: int = 90) -> tuple[list[dict], dict]:
    """Fetch daily bars + quote from Financial Modeling Prep."""
    import requests  # lazy

    base = "https://financialmodelingprep.com/api/v3"
    hist_url = f"{base}/historical-price-full/{ticker}"
    hist_resp = requests.get(hist_url, params={"timeseries": days, "apikey": api_key}, timeout=30)
    hist_resp.raise_for_status()
    payload = hist_resp.json() or {}
    # FMP returns most-recent-first; reverse to chronological order.
    rows = list(reversed(payload.get("historical", [])))
    bars = [{"close": float(r["close"]), "volume": float(r.get("volume") or 0)} for r in rows]

    quote_url = f"{base}/quote/{ticker}"
    quote_resp = requests.get(quote_url, params={"apikey": api_key}, timeout=30)
    quote_resp.raise_for_status()
    q_list = quote_resp.json() or []
    q = q_list[0] if q_list else {}
    quote = {
        "bid": q.get("bid"),
        "ask": q.get("ask"),
        "current_price": q.get("price"),
        "shares_outstanding": q.get("sharesOutstanding"),
        "float_shares": None,
    }
    return bars, quote


def load_bars_json(path: str) -> dict:
    """Load a pre-fetched bars file: {ticker: {"bars": [...], "quote": {...}}}."""
    with open(path) as f:
        return json.load(f)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pre-trade liquidity and execution-cost check")
    parser.add_argument("tickers", nargs="*", help="Ticker symbol(s) to analyze")
    parser.add_argument("--order-shares", type=float, help="Order size in shares")
    parser.add_argument("--order-dollars", type=float, help="Order size in dollars")
    parser.add_argument(
        "--source",
        choices=["yfinance", "fmp"],
        default="yfinance",
        help="Data source (default: yfinance, keyless)",
    )
    parser.add_argument("--period", default="3mo", help="yfinance history period (default: 3mo)")
    parser.add_argument("--fmp-days", type=int, default=90, help="FMP history length in days")
    parser.add_argument("--api-key", help="FMP API key (or set FMP_API_KEY)")
    parser.add_argument(
        "--bars-json",
        help="Offline: JSON file of {ticker: {bars, quote}} instead of fetching",
    )
    parser.add_argument("--output-dir", default="reports/", help="Report output dir")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    bars_data: dict | None = None
    if args.bars_json:
        bars_data = load_bars_json(args.bars_json)

    tickers = args.tickers or (list(bars_data.keys()) if bars_data else [])
    if not tickers:
        parser.error("provide at least one ticker (or --bars-json)")

    api_key = args.api_key or os.environ.get("FMP_API_KEY")
    if args.source == "fmp" and not bars_data and not api_key:
        print(
            "Error: FMP source requires --api-key or FMP_API_KEY environment variable",
            file=sys.stderr,
        )
        sys.exit(1)

    results: list[dict] = []
    for ticker in tickers:
        try:
            if bars_data is not None:
                entry = bars_data.get(ticker, {})
                bars = entry.get("bars", [])
                quote = entry.get("quote", {})
            elif args.source == "fmp":
                bars, quote = fetch_fmp(ticker, api_key, days=args.fmp_days)
            else:
                bars, quote = fetch_yfinance(ticker, period=args.period)
        except Exception as e:  # noqa: BLE001 - surface fetch errors per ticker
            results.append({"ticker": ticker, "error": str(e)})
            continue
        results.append(
            analyze_ticker(
                ticker,
                bars,
                quote=quote,
                order_shares=args.order_shares,
                order_dollars=args.order_dollars,
            )
        )

    report = build_report(results, args.order_shares, args.order_dollars)

    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    json_path = os.path.join(args.output_dir, f"liquidity_check_{timestamp}.json")
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"JSON report: {json_path}")

    md_path = os.path.join(args.output_dir, f"liquidity_check_{timestamp}.md")
    with open(md_path, "w") as f:
        f.write(generate_markdown_report(report))
    print(f"Markdown report: {md_path}")

    for r in results:
        if "error" in r:
            print("{}: ERROR {}".format(r["ticker"], r["error"]))
            continue
        slip = (
            "{} bps ({})".format(r["estimated_slippage_bps"], r["slippage_grade"])
            if r["estimated_slippage_bps"] is not None
            else "n/a (no order size)"
        )
        print("{}: liquidity={}, slippage={}".format(r["ticker"], r["liquidity_grade"], slip))


if __name__ == "__main__":
    main()
