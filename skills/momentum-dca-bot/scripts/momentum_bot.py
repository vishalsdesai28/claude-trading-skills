#!/usr/bin/env python3
"""Daily momentum DCA bot: momentum-pullback screen -> top-1 pick -> fixed-notional buy.

Strategy (institutional-style, fully mechanical):
  Screen : momentum pullback — YTD +50%, above 20/50/200 SMA, red week, mid-cap or
           larger, within 10% of 52-week high, analyst buy-or-better. Default source
           is Yahoo Finance (keyless via yfinance); Finviz Elite API or a manual
           Finviz CSV export also supported.
  Rank   : Perf Month desc, tiebreak Perf Week desc (shallowest pullback wins).
  Buy    : $300 notional/day into the top-ranked name, max 3 lots ($900) per name,
           one buy per trading day, only while SPY closes above its 50DMA (regime gate).
  Book   : sell HALF a position once it closes >= +20% over average entry (once per
           name). After that, the 15% trail mathematically locks the remainder in
           above breakeven on a daily-close basis (0.85 x 1.20 = 1.02).
  Exit   : liquidate a name when its daily close breaks below the 50DMA, or falls
           15% off the high-water close since entry, whichever hits first.

Broker is Alpaca; PAPER by default (set ALPACA_PAPER=false for live). --dry-run
never sends orders. Every action is appended to state/momentum_bot/trades.jsonl.

Usage:
  python3 momentum_bot.py scan   [--source yahoo|finviz] [--csv export.csv] [--top 5]
  python3 momentum_bot.py buy    [--dry-run] [--budget 300] [--skip-regime-check]
  python3 momentum_bot.py manage [--dry-run]
  python3 momentum_bot.py status
"""

import argparse
import csv
import io
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests library not found. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parents[3]
STATE_DIR = REPO_ROOT / "state" / "momentum_bot"
STATE_FILE = STATE_DIR / "positions.json"
TRADES_LOG = STATE_DIR / "trades.jsonl"
REPORTS_DIR = REPO_ROOT / "reports"

FINVIZ_EXPORT_URL = "https://elite.finviz.com/export.ashx"
# Finviz preset, tightened: cap_large -> cap_midover (mid-to-mega, not just $10-200B),
# avg volume 200K -> 500K, plus within-10%-of-52wk-high for momentum quality.
FINVIZ_FILTERS = (
    "an_recom_buybetter,cap_midover,ind_stocksonly,sh_avgvol_o500,sh_price_o10,"
    "ta_highlow52w_b0to10h,ta_perf_ytd50o,ta_perf2_1wdown,"
    "ta_sma20_pa,ta_sma200_pa,ta_sma50_pa"
)
FINVIZ_VIEW = "141"  # Performance view: has Perf Week / Perf Month columns

# Listed-exchange whitelist for the Yahoo path (drops OTC/ADR pink-sheet noise).
YAHOO_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE"}

DEFAULT_BUDGET = 300.0
MAX_LOTS_PER_NAME = 3
TRAIL_PCT = 0.15
SMA_EXIT_PERIOD = 50
REGIME_SMA_PERIOD = 50
MIN_YTD_PERF = 50.0
HIGH_PROXIMITY = 0.90  # last close must be >= 90% of 52-week high
SCALE_OUT_GAIN = 0.20  # book profits at +20% over average entry
SCALE_OUT_FRAC = 0.50  # ... by selling half


# ---------------------------------------------------------------- pure logic


def _num(value):
    """Parse Finviz CSV cell like '12.34%' / '1,234.56' / '-' -> float or None."""
    try:
        return float(str(value).replace("%", "").replace(",", "").strip())
    except ValueError:
        return None


def _find_col(fieldnames, *words):
    """Case-insensitive contains-all header match ('Perf Month' vs 'Performance (Month)')."""
    for name in fieldnames or []:
        low = name.lower()
        if all(w in low for w in words):
            return name
    return None


def rank_candidates(rows):
    """Rank Finviz CSV rows: Perf Month desc, tiebreak Perf Week desc (shallowest pullback)."""
    if not rows:
        return []
    fields = list(rows[0].keys())
    ticker_col = _find_col(fields, "ticker")
    month_col = _find_col(fields, "perf", "month")
    week_col = _find_col(fields, "perf", "week")
    price_col = _find_col(fields, "price")
    if not (ticker_col and month_col and week_col):
        raise SystemExit(
            "ERROR: CSV missing Ticker/Perf Week/Perf Month columns. "
            "Export the Performance view (v=141)."
        )
    ranked = []
    for row in rows:
        ticker = (row.get(ticker_col) or "").strip()
        month = _num(row.get(month_col))
        if not ticker or month is None:
            continue
        ranked.append(
            {
                "ticker": ticker,
                "perf_month": month,
                "perf_week": _num(row.get(week_col)) or 0.0,
                "price": _num(row.get(price_col)) if price_col else None,
            }
        )
    ranked.sort(key=lambda r: (-r["perf_month"], -r["perf_week"]))
    return ranked


def prefilter_quote(quote, high_frac=HIGH_PROXIMITY, max_rating=2.5):
    """Quote-level filters on a Yahoo screen result (before downloading history):
    listed exchange, price above 50/200DMA, within 10% of 52-week high,
    analyst consensus buy-or-better (1=Strong Buy .. 5=Sell; missing = keep)."""
    price = quote.get("regularMarketPrice")
    sma50 = quote.get("fiftyDayAverage")
    sma200 = quote.get("twoHundredDayAverage")
    high52 = quote.get("fiftyTwoWeekHigh")
    values = (price, sma50, sma200, high52)
    if not all(isinstance(v, (int, float)) for v in values):
        return False
    if quote.get("exchange") not in YAHOO_EXCHANGES:
        return False
    if not (price > sma50 and price > sma200 and price >= high_frac * high52):
        return False
    rating = str(quote.get("averageAnalystRating") or "").split(" ")[0]
    try:
        if float(rating) > max_rating:
            return False
    except ValueError:
        pass
    return True


def momentum_metrics(rows, min_ytd=MIN_YTD_PERF, high_frac=HIGH_PROXIMITY):
    """History-level filters + ranking metrics. `rows` is [(date, close)] newest first,
    ~1 year. Returns metrics dict, or None if any momentum-pullback filter fails."""
    if len(rows) < 210:
        return None  # needs SMA200 + a month of lookback; young listings are skipped
    dates = [r[0] for r in rows]
    closes = [r[1] for r in rows]
    last = closes[0]
    if not (last > sma(closes, 20) and last > sma(closes, 50) and last > sma(closes, 200)):
        return None
    perf_week = (last / closes[5] - 1) * 100
    if perf_week >= 0:
        return None  # not a pullback week
    if last < high_frac * max(closes[:252]):
        return None
    idx = 0  # find prior-year close for the YTD base (falls back to oldest row)
    while idx < len(rows) and dates[idx].year == dates[0].year:
        idx += 1
    ytd = (last / closes[min(idx, len(closes) - 1)] - 1) * 100
    if ytd < min_ytd:
        return None
    perf_month = (last / closes[21] - 1) * 100
    return {
        "perf_month": round(perf_month, 2),
        "perf_week": round(perf_week, 2),
        "perf_ytd": round(ytd, 2),
        "price": round(last, 2),
    }


def pick_candidate(ranked, positions, max_lots=MAX_LOTS_PER_NAME):
    """First ranked name whose lot count is below the per-name cap."""
    for cand in ranked:
        if len(positions.get(cand["ticker"], {}).get("lots", [])) < max_lots:
            return cand
    return None


def sma(closes_desc, period):
    """Simple moving average over the most recent `period` closes (newest first)."""
    if len(closes_desc) < period:
        return None
    return sum(closes_desc[:period]) / period


def regime_ok(spy_closes_desc, period=REGIME_SMA_PERIOD):
    """Risk-on only when SPY's latest close is above its 50DMA. Fails closed."""
    avg = sma(spy_closes_desc, period)
    return avg is not None and spy_closes_desc[0] > avg


def should_exit(closes_desc, hwm, period=SMA_EXIT_PERIOD, trail=TRAIL_PCT):
    """Return exit reason or None. `hwm` must already include today's close."""
    close = closes_desc[0]
    avg = sma(closes_desc, period)
    if avg is not None and close < avg:
        return f"close_below_sma{period}"
    if close <= hwm * (1 - trail):
        return f"trailing_stop_{int(trail * 100)}pct"
    return None


def avg_entry(pos):
    """Notional-weighted average entry price from recorded lots; None if unknown."""
    # ponytail: estimated from screen-time prices; swap in Alpaca's avg_entry_price
    # if penny accuracy ever matters.
    lots = pos.get("lots", [])
    if not lots or any(not lot.get("price") for lot in lots):
        return None
    shares = sum(lot["notional"] / lot["price"] for lot in lots)
    return sum(lot["notional"] for lot in lots) / shares


def should_scale_out(close, entry, already_scaled, gain=SCALE_OUT_GAIN):
    """Book profits (sell half) the first time a name closes >= +20% over entry."""
    return not already_scaled and entry is not None and close >= entry * (1 + gain)


# ---------------------------------------------------------------- data fetch


def yahoo_candidates(size=250):
    """Keyless screen via Yahoo Finance: coarse EquityQuery server-side, then
    quote-level prefilter, then exact momentum-pullback filters on 1y history."""
    import yfinance as yf  # lazy: pure logic above stays importable offline

    Q = yf.EquityQuery
    query = Q(
        "and",
        [
            Q("eq", ["region", "us"]),
            Q("gt", ["intradayprice", 10]),
            Q("gt", ["intradaymarketcap", 2_000_000_000]),
            Q("gt", ["avgdailyvol3m", 500_000]),
            Q("gt", ["fiftytwowkpercentchange", MIN_YTD_PERF]),
        ],
    )
    res = yf.screen(query, sortField="fiftytwowkpercentchange", sortAsc=False, size=size)
    quotes = res.get("quotes", [])
    total = res.get("total", len(quotes))
    if total > len(quotes):
        print(
            f"NOTE: Yahoo screen truncated to top {len(quotes)} of {total} by 52-week momentum.",
            file=sys.stderr,
        )
    survivors = [q["symbol"] for q in quotes if prefilter_quote(q)]
    print(
        f"Yahoo screen: {len(quotes)} coarse -> {len(survivors)} after prefilter", file=sys.stderr
    )
    ranked = []
    for symbol in survivors:
        try:
            metrics = momentum_metrics(daily_rows(symbol))
        except Exception as exc:  # one bad ticker must not kill the daily run
            print(f"WARN: skipping {symbol}: {exc}", file=sys.stderr)
            continue
        if metrics:
            ranked.append({"ticker": symbol, **metrics})
    ranked.sort(key=lambda r: (-r["perf_month"], -r["perf_week"]))
    return ranked


def daily_rows(symbol):
    """[(date, close)] newest first, ~1 year, via yfinance (keyless)."""
    import yfinance as yf

    hist = yf.Ticker(symbol).history(period="1y", auto_adjust=True)
    if hist.empty:
        raise SystemExit(f"ERROR: no Yahoo price history for {symbol}.")
    rows = [(idx.date(), float(close)) for idx, close in hist["Close"].dropna().items()]
    rows.reverse()
    return rows


def daily_closes(symbol):
    return [close for _, close in daily_rows(symbol)]


def fetch_finviz_rows(api_key):
    """Fetch the screen as CSV rows via the Finviz Elite export API."""
    params = {"v": FINVIZ_VIEW, "f": FINVIZ_FILTERS, "ft": "4", "o": "-perf4w", "auth": api_key}
    resp = requests.get(FINVIZ_EXPORT_URL, params=params, timeout=30)
    if resp.status_code in (401, 403):
        raise SystemExit("ERROR: Finviz auth failed — check FINVIZ_API_KEY (Elite required).")
    resp.raise_for_status()
    return list(csv.DictReader(io.StringIO(resp.content.decode("utf-8"))))


def get_candidates(args):
    """Ranked candidates from --csv (manual Finviz export), Finviz Elite, or Yahoo."""
    if getattr(args, "csv", None):
        return rank_candidates(list(csv.DictReader(Path(args.csv).open())))
    if args.source == "finviz":
        api_key = os.getenv("FINVIZ_API_KEY")
        if not api_key:
            raise SystemExit("ERROR: --source finviz needs FINVIZ_API_KEY (or use --csv).")
        return rank_candidates(fetch_finviz_rows(api_key))
    return yahoo_candidates()


# ---------------------------------------------------------------- broker


def _alpaca():
    """(base_url, headers, is_paper) — paper unless ALPACA_PAPER=false."""
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not (key and secret):
        raise SystemExit("ERROR: set ALPACA_API_KEY and ALPACA_SECRET_KEY (paper account first).")
    # ALPACA_PAPER is this repo's convention; ALPACA_PAPER_TRADE is alpaca-mcp-server's
    paper_env = os.getenv("ALPACA_PAPER") or os.getenv("ALPACA_PAPER_TRADE") or "true"
    paper = paper_env.lower() != "false"
    base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
    return base, {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, paper


def alpaca_market_open():
    base, headers, _ = _alpaca()
    resp = requests.get(f"{base}/v2/clock", headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json().get("is_open", False)


def alpaca_buy_notional(ticker, notional):
    base, headers, _ = _alpaca()
    resp = requests.post(
        f"{base}/v2/orders",
        headers=headers,
        json={
            "symbol": ticker,
            "notional": f"{notional:.2f}",
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def alpaca_close_position(ticker, percentage=None):
    """Liquidate a position (whole, or `percentage` of it). False if broker doesn't hold it."""
    base, headers, _ = _alpaca()
    params = {"percentage": str(percentage)} if percentage else None
    resp = requests.delete(
        f"{base}/v2/positions/{ticker}", headers=headers, params=params, timeout=30
    )
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return True


# ---------------------------------------------------------------- state


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_buy_date": None, "positions": {}}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def log_trade(**fields):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fields["ts"] = datetime.now(timezone.utc).isoformat()
    with TRADES_LOG.open("a") as fh:
        fh.write(json.dumps(fields) + "\n")


def scan_path(day):
    return REPORTS_DIR / f"momentum_scan_{day}.json"


def save_scan(ranked, day):
    """Persist the day's full ranked candidate list — audit trail of every screen run."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    scan_path(day).write_text(json.dumps({"date": day, "candidates": ranked}, indent=2) + "\n")


def load_saved_scan(day):
    """Ranked candidates from an earlier same-day scan, or None if none was saved."""
    path = scan_path(day)
    if not path.exists():
        return None
    return json.loads(path.read_text())["candidates"]


# ---------------------------------------------------------------- commands


def cmd_scan(args):
    today = date.today().isoformat()
    ranked = get_candidates(args)
    save_scan(ranked, today)
    print(json.dumps({"date": today, "candidates": ranked[: args.top]}, indent=2))
    print(f"Saved full scan to {scan_path(today)}", file=sys.stderr)
    return 0


def cmd_buy(args):
    today = date.today().isoformat()
    state = load_state()
    if state.get("last_buy_date") == today:
        print(f"Already bought today ({today}); skipping.")
        return 0
    if not args.dry_run and not alpaca_market_open():
        print("Market closed; skipping (prevents queued orders stacking on holidays).")
        return 0
    if not args.skip_regime_check:
        if not regime_ok(daily_closes("SPY")):
            print("Regime gate OFF (SPY close <= 50DMA): no buy today. Exits still active.")
            log_trade(action="skip_buy", reason="regime_off")
            return 0
    ranked = None if args.csv else load_saved_scan(today)
    if ranked is not None:
        print(f"Using this morning's saved scan ({scan_path(today).name}).", file=sys.stderr)
    else:
        ranked = get_candidates(args)
        save_scan(ranked, today)
    cand = pick_candidate(ranked, state["positions"])
    if cand is None:
        print("No eligible candidate (screen empty or all names at 3-lot cap); skipping.")
        log_trade(action="skip_buy", reason="no_candidate")
        return 0
    if args.dry_run:
        print(
            f"[dry-run] would buy ${args.budget:.2f} of {cand['ticker']} "
            f"(perf_month {cand['perf_month']}%, perf_week {cand['perf_week']}%)"
        )
        return 0
    order = alpaca_buy_notional(cand["ticker"], args.budget)
    pos = state["positions"].setdefault(cand["ticker"], {"lots": [], "hwm": 0.0})
    pos["lots"].append({"date": today, "notional": args.budget, "price": cand.get("price")})
    pos["hwm"] = max(pos["hwm"], cand.get("price") or 0.0)
    state["last_buy_date"] = today
    save_state(state)
    log_trade(
        action="buy",
        ticker=cand["ticker"],
        notional=args.budget,
        order_id=order.get("id"),
        paper=_alpaca()[2],
    )
    print(f"BOUGHT ${args.budget:.2f} {cand['ticker']} (order {order.get('id')})")
    return 0


def cmd_manage(args):
    state = load_state()
    if not state["positions"]:
        print("No open positions.")
        return 0
    for ticker in list(state["positions"]):
        pos = state["positions"][ticker]
        closes = daily_closes(ticker)
        pos["hwm"] = max(pos.get("hwm", 0.0), closes[0])
        reason = should_exit(closes, pos["hwm"])
        if reason:
            if args.dry_run:
                print(f"[dry-run] would SELL ALL {ticker}: {reason}")
                continue
            held = alpaca_close_position(ticker)
            if not held:
                print(f"WARN: {ticker} not held at broker; clearing from state.", file=sys.stderr)
            del state["positions"][ticker]
            log_trade(action="sell_all", ticker=ticker, reason=reason, broker_held=held)
            print(f"SOLD ALL {ticker}: {reason}")
            continue
        entry = avg_entry(pos)
        if should_scale_out(closes[0], entry, pos.get("scaled_out", False)):
            gain_pct = (closes[0] / entry - 1) * 100
            if args.dry_run:
                print(f"[dry-run] would BOOK PROFITS on {ticker}: sell half at +{gain_pct:.1f}%")
                continue
            alpaca_close_position(ticker, percentage=int(SCALE_OUT_FRAC * 100))
            pos["scaled_out"] = True
            log_trade(action="scale_out", ticker=ticker, gain_pct=round(gain_pct, 2))
            print(f"BOOKED PROFITS {ticker}: sold half at +{gain_pct:.1f}% over entry")
            continue
        print(f"HOLD {ticker}: close {closes[0]:.2f}, hwm {pos['hwm']:.2f}")
    if not args.dry_run:
        save_state(state)
    return 0


def cmd_status(_args):
    print(json.dumps(load_state(), indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add_source_args(p):
        p.add_argument("--source", choices=["yahoo", "finviz"], default="yahoo")
        p.add_argument("--csv", help="path to a manual Finviz CSV export (overrides --source)")

    scan = sub.add_parser("scan", help="rank screener candidates")
    add_source_args(scan)
    scan.add_argument("--top", type=int, default=5)
    scan.set_defaults(func=cmd_scan)

    buy = sub.add_parser("buy", help="buy today's $300 lot")
    add_source_args(buy)
    buy.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    buy.add_argument("--dry-run", action="store_true")
    buy.add_argument("--skip-regime-check", action="store_true")
    buy.set_defaults(func=cmd_buy)

    manage = sub.add_parser("manage", help="book profits at +20%, liquidate broken names")
    manage.add_argument("--dry-run", action="store_true")
    manage.set_defaults(func=cmd_manage)

    status = sub.add_parser("status", help="print bot state")
    status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
