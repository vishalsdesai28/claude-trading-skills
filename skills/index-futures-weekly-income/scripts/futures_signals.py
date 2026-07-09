#!/usr/bin/env python3
"""Generate ES/NQ trade signals (core trend position + weekly satellites).

Data: Yahoo Finance via yfinance (keyless), or an offline JSON fixture.

Usage:
    python3 futures_signals.py                       # writes reports/index-futures-weekly/
    python3 futures_signals.py --symbols ES --account-size 250000
    python3 futures_signals.py --fixture tests/fixture.json --output-dir /tmp/x
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from signal_engine import CONTRACTS, build_signals  # noqa: E402

LOOKBACK_DAYS = 320  # enough for the 200d SMA + 52-week high with margin
DEFAULT_OUTPUT_DIR = "reports/index-futures-weekly/"


def fetch_live(symbol):
    import yfinance as yf

    c = CONTRACTS[symbol]
    df = yf.Ticker(c["yahoo"]).history(period="2y", interval="1d", auto_adjust=False)
    if df.empty:
        raise SystemExit(f"No data returned for {c['yahoo']}")
    bars = [
        {
            "date": idx.date().isoformat(),
            "open": float(r["Open"]),
            "high": float(r["High"]),
            "low": float(r["Low"]),
            "close": float(r["Close"]),
        }
        for idx, r in df.iterrows()
    ][-LOOKBACK_DAYS:]
    vix = None
    for vsym in (c["vix_symbol"], "^VIX"):
        vdf = yf.Ticker(vsym).history(period="5d", interval="1d")
        if not vdf.empty:
            vix = float(vdf["Close"].iloc[-1])
            break
    if vix is None:
        raise SystemExit("Could not fetch a volatility index (^VIX/^VXN)")
    return bars, vix


def handoff_block(symbol, sig, json_path):
    """Per-signal handoff commands for downstream skills."""
    c = CONTRACTS[symbol]
    h = {
        "technical_analyst": (
            f"Confirm the {sig['setup']} setup on a daily/weekly {c['yahoo']} chart "
            "before entry (trend structure, volume, nearby S/R)."
        ),
        "trader_memory_core": (
            "python3 skills/trader-memory-core/scripts/trader_memory_cli.py ingest "
            f"--source index-futures-weekly-income --input {json_path} --state-dir state/theses/"
        ),
    }
    if sig.get("instrument") == "future" and "entry" in sig:
        pv = c["micro_point_value"]
        h["position_sizer"] = (
            "python3 skills/position-sizer/scripts/position_sizer.py "
            f"--entry {sig['entry'] * pv:.2f} --stop {sig['stop'] * pv:.2f} "
            "--account-size <ACCOUNT> --risk-pct 1.0  "
            f"# prices pre-multiplied by {c['micro']} point value (${pv}/pt): "
            f"1 'share' = 1 {c['micro']} contract"
        )
    return h


def front_month(today):
    """Approximate front quarterly contract (Mar/Jun/Sep/Dec) — confirm at broker."""
    for m in (3, 6, 9, 12):
        if today.month <= m:
            return f"{('Mar', 'Jun', 'Sep', 'Dec')[(m // 3) - 1]} {today.year}"
    return f"Mar {today.year + 1}"


def nearest_friday(d):
    """Friday closest to date d — ES/NQ list weekly + EOM options on every Friday,
    so the ~30-DTE trade should use the Friday nearest the target, NOT the next
    quarterly/monthly (3rd-Friday) expiry, which can be 45+ days out."""
    shift = (4 - d.weekday()) % 7  # next Friday
    back = shift - 7  # previous Friday
    import datetime as _dt

    return d + _dt.timedelta(days=shift if shift <= -back else back)


def _fmt_pts(x):
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def render_md(payload, account, core_lev):
    today = datetime.date.fromisoformat(payload["week_of"])
    expiry = (today + datetime.timedelta(days=4)).isoformat()  # Friday of the trade week
    lines = [
        "# Index Futures Weekly Income — Trade Plan",
        "",
        f"**Generated:** {payload['generated_at']}  •  **For the week of:** {payload['week_of']}"
        f"  •  **Assumes:** ${account:,.0f} account, {core_lev}x core leverage, 1% risk per satellite trade",
        "",
        "> Educational analysis, not financial advice. Futures and options involve",
        "> substantial risk of loss. Estimated option credits are model prices —",
        "> check the live quote before entering.",
        "",
    ]
    for ctx in payload["markets"]:
        sym = ctx["symbol"]
        c = CONTRACTS[sym]
        pv, mpv, micro = c["point_value"], c["micro_point_value"], c["micro"]
        fm = front_month(today)
        n_trade = 0
        lines += [
            f"## {sym} — {ctx['index']} futures",
            "",
            f"**Market picture:** {sym} closed at **{ctx['spot']:,}**. Trend is "
            f"**{ctx['regime']['trend'].upper()}**, volatility is **{ctx['regime']['vol'].upper()}** "
            f"(vol index {ctx['vix']}). Options imply a move of about ±{ctx['expected_move_1w']:,.0f} "
            "points over the coming week.",
            "",
        ]
        for sig in ctx["signals"]:
            setup = sig["setup"]
            if setup == "core_trend_position":
                n_trade += 1
                contracts = round(account * core_lev / (ctx["spot"] * mpv))
                lines += [f"### Trade {n_trade} — Core position: **{sig['state']}**", ""]
                if sig["state"] == "LONG":
                    lines += [
                        "**In plain language:** the long-term trend is healthy, so stay invested. "
                        "This position does the compounding; it has no stop-loss — one exit line, "
                        "checked once a week at Friday's close.",
                        "",
                        "- Already long: **HOLD — do nothing this week.**",
                        f"- Starting fresh: **BUY {contracts} {micro} ({fm})** at market "
                        f"(≈{core_lev}x exposure on ${account:,.0f}; "
                        f"{micro} = ${mpv}/point, ≈${ctx['spot'] * mpv:,.0f} notional each).",
                        f"- **EXIT everything only if Friday closes below {sig['exit_trigger']:,}** "
                        f"(price is {sig['distance_pct']:+.1f}% above the 200-day average "
                        f"at {sig['entry_trigger']:,}; crash line {sig['crash_line']:,}).",
                    ]
                elif sig["state"] == "HOLD_IF_LONG":
                    lines += [
                        "**In plain language:** price slipped just under the long-term average — "
                        "inside the tolerance channel. Keep what you have, don't add.",
                        "",
                        f"- Already long: **HOLD** unless Friday closes below {sig['exit_trigger']:,}.",
                        f"- Not long: **stay out** until a Friday close back above {sig['entry_trigger']:,}.",
                    ]
                else:
                    lines += [
                        "**In plain language:** the long-term trend is broken. Stay in cash on "
                        "the core position — missing crashes is where this system wins.",
                        "",
                        f"- **Stay flat.** Re-enter only on a Friday close above {sig['entry_trigger']:,}.",
                    ]
            elif setup in ("pullback_continuation", "weekly_breakout") and "entry" in sig:
                n_trade += 1
                risk = sig["risk_per_contract"][micro]
                n_con = max(int(account * 0.01 / risk), 0)
                title = "Dip-buy" if setup == "pullback_continuation" else "Breakout buy"
                lines += [
                    f"### Trade {n_trade} — {title} (satellite, futures)",
                    "",
                    "**In plain language:** "
                    + (
                        "the market is trending up; place a resting order to buy a dip to the "
                        "20-day average, risking about 1% of the account."
                        if setup == "pullback_continuation"
                        else "if the market pushes above last week's high, join the move. This "
                        "setup has the weakest edge — skip it unless a chart check (technical-"
                        "analyst) confirms it."
                    ),
                    "",
                    f"**Exact orders ({micro} {fm}, ${mpv}/point):**",
                    f"1. Place **{sig['order'].upper()}** at **{_fmt_pts(sig['entry'])}**, "
                    "good this week only.",
                    f"2. If filled, set **STOP at {_fmt_pts(sig['stop'])}** "
                    f"(−${risk:,.0f} per contract) and **TARGET at {_fmt_pts(sig['target'])}** "
                    f"(+${abs(sig['target'] - sig['entry']) * mpv:,.0f} per contract, "
                    f"{sig['rr']}:1 reward:risk).",
                    "3. Not filled, stopped, or still open at Friday's close → cancel/close at market.",
                    f"- Size for 1% risk (${account * 0.01:,.0f}): **{n_con} contract(s)**."
                    + (" Too big for this account — skip." if n_con == 0 else ""),
                ]
            elif setup == "monthly_bull_put_spread":
                n_trade += 1
                cr_d = sig["est_credit"] * pv
                ml_d = sig["max_loss"] * pv
                n_spreads = max(int(account * 0.05 / ml_d), 0)
                m_exp_date = nearest_friday(today + datetime.timedelta(days=sig["dte"]))
                m_expiry = f"{m_exp_date.isoformat()} ({(m_exp_date - today).days} days out)"
                lines += [
                    f"### Trade {n_trade} — Monthly income: bull put spread at the money (options)",
                    "",
                    f"**In plain language:** sell a put right at the current price and buy one "
                    f"an expected move lower. You win if {sym} is flat or up a month from now "
                    "(historically ~73-74% of months). Premium is much richer than the weekly "
                    "trade, and losses are capped — but they are real and roughly 2x the wins.",
                    "",
                    f"**Exact orders ({sym} weekly/EOM options expiring Fri {m_expiry}, ${pv}/point):**",
                    f"1. **SELL 1 put, strike {sig['short_strike']:,}** (at the money)",
                    f"2. **BUY 1 put, strike {sig['long_strike']:,}** (same expiry)",
                    f"- Net credit ≈ {sig['est_credit']} pts = **${cr_d:,.0f} per spread** — max "
                    f"profit if {sym} closes above {sig['short_strike']:,} at expiry.",
                    f"- Max loss **${ml_d:,.0f}** per spread below {sig['long_strike']:,}. "
                    f"Breakeven {sig['breakeven']:,}.",
                    f"- After entry, work two GTC buy-to-close orders: **take profit at "
                    f"≈{sig['management']['profit_take_value']} pts** (${sig['management']['profit_take_value'] * pv:,.0f} — half the credit; "
                    "if filled with ≥7 days left, sell a fresh at-the-money spread) and "
                    f"**stop at ≈{sig['management']['stop_value']} pts** "
                    f"(${sig['management']['stop_value'] * pv:,.0f} — 2x credit; if hit, no re-entry until next month).",
                    f"- Cap total max-loss at 5% of account: **{n_spreads} spread(s)**."
                    + (
                        " Too big for this account — use the smaller proxy below."
                        if n_spreads == 0
                        else ""
                    ),
                    f"- Smaller-size alternatives: {', '.join(sig['option_proxies'][1:])}"
                    + (
                        f" (XSP ≈ SELL {round(sig['short_strike'] / 10)} put / BUY "
                        f"{round(sig['long_strike'] / 10)} put, $100/pt)"
                        if sym == "ES"
                        else f" (QQQ ≈ SELL {round(sig['short_strike'] / 41)} put / BUY "
                        f"{round(sig['long_strike'] / 41)} put, $100/pt)"
                    ),
                ]
            elif setup == "put_credit_spread":
                n_trade += 1
                cr_d = sig["est_credit"] * pv
                ml_d = sig["max_loss"] * pv
                n_spreads = max(int(account * 0.02 / ml_d), 0)
                lines += [
                    f"### Trade {n_trade} — Weekly income: put credit spread (options)",
                    "",
                    f"**In plain language:** sell insurance one expected move below the market. "
                    f"If {sym} stays above {sig['short_strike']:,} through Friday, keep the "
                    "premium (~88-90% of weeks historically). Wins are small and losses are "
                    "big but capped — never oversize.",
                    "",
                    f"**Exact orders ({sym} weekly options expiring Fri {expiry}, ${pv}/point):**",
                    f"1. **SELL 1 put, strike {sig['short_strike']:,}**",
                    f"2. **BUY 1 put, strike {sig['long_strike']:,}** (same expiry)",
                    f"- Net credit ≈ {sig['est_credit']} pts = **${cr_d:,.0f} per spread** (model "
                    "estimate — enter as a limit at the live mid).",
                    f"- Max loss **${ml_d:,.0f}** per spread if {sym} closes below "
                    f"{sig['long_strike']:,} at expiry. Breakeven {sig['breakeven']:,}.",
                    f"- Hold to expiry. Cap total max-loss at 2% of account: **{n_spreads} spread(s)**."
                    + (
                        " Too big for this account — use the smaller proxy below."
                        if n_spreads == 0
                        else ""
                    ),
                    f"- Smaller-size alternatives: {', '.join(sig['option_proxies'][1:])}"
                    + (
                        f" (XSP ≈ SELL {round(sig['short_strike'] / 10)} put / BUY "
                        f"{round(sig['long_strike'] / 10)} put, $100/pt)"
                        if sym == "ES"
                        else f" (QQQ ≈ SELL {round(sig['short_strike'] / 41)} put / BUY "
                        f"{round(sig['long_strike'] / 41)} put, $100/pt)"
                    ),
                ]
            else:  # stand-aside notes
                lines += [
                    f"### {setup.replace('_', ' ').title()}",
                    "",
                    f"**{sig.get('note', '')}**",
                ]
            hs = sig.get("handoff", {})
            if hs:
                lines.append("")
                lines += [f"- handoff → {k}: `{v}`" for k, v in hs.items()]
            lines.append("")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", nargs="+", default=["ES", "NQ"], choices=list(CONTRACTS))
    p.add_argument("--fixture", help="Offline JSON: {SYM: {bars: [...], vix: float}}")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--account-size", type=float, default=100_000)
    p.add_argument("--core-leverage", type=float, default=1.5)
    args = p.parse_args()

    fixture = json.loads(Path(args.fixture).read_text()) if args.fixture else None
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    json_path = outdir / f"index_futures_signals_{stamp}.json"

    markets = []
    for sym in args.symbols:
        if fixture:
            bars, vix = fixture[sym]["bars"], fixture[sym]["vix"]
        else:
            bars, vix = fetch_live(sym)
        ctx = build_signals(sym, bars, vix)
        for sig in ctx["signals"]:
            sig["id"] = f"{sym}-{sig['setup']}-{stamp}"
            if sig["instrument"] != "note":  # stand-aside needs no handoff
                sig["handoff"] = handoff_block(sym, sig, json_path)
        markets.append(ctx)

    today = datetime.date.today()
    payload = {
        "skill": "index-futures-weekly-income",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "week_of": (today + datetime.timedelta(days=(7 - today.weekday()) % 7)).isoformat(),
        "markets": markets,
    }
    json_path.write_text(json.dumps(payload, indent=2))
    md = render_md(payload, args.account_size, args.core_leverage)
    md_path = outdir / f"index_futures_signals_{stamp}.md"
    md_path.write_text(md)
    print(md)
    print(f"Saved to {json_path} and {md_path}")


if __name__ == "__main__":
    main()
