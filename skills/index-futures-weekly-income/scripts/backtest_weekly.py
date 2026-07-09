#!/usr/bin/env python3
"""Weekly backtest for the index-futures weekly-income rule set.

Pure simulation functions (testable offline) + a Yahoo Finance CLI.
Conservative fill model: on any bar that touches both stop and target,
the trade is counted as a stop-out.

Usage:
    python3 backtest_weekly.py --symbols ES NQ --start 2010-01-01 --output-dir reports/
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from signal_engine import (  # noqa: E402
    ATR_N,
    BUFFER_ATR,
    CONTRACTS,
    CORE_TREND_N,
    EM_SHORT,
    EM_WING,
    STOP_ATR,
    TARGET_ATR,
    VIX_STRESSED,
    _round_strike,
    _spread_credit,
    atr,
    classify_trend,
    ema,
    expected_move,
    sma,
)


def atr_at(bars, n=ATR_N):
    return atr(bars, n)


def weekly_groups(bars):
    """Group daily bars into ISO-calendar weeks, oldest first."""
    weeks, cur, cur_key = [], [], None
    for b in bars:
        d = datetime.date.fromisoformat(b["date"])
        key = d.isocalendar()[:2]
        if key != cur_key and cur:
            weeks.append(cur)
            cur = []
        cur_key = key
        cur.append(b)
    if cur:
        weeks.append(cur)
    return weeks


def _manage_long(week_bars, start_idx, fill, stop, target):
    """Walk bars from fill onward; stop checked before target (conservative)."""
    for b in week_bars[start_idx:]:
        if b["low"] <= stop:
            return b["date"], stop, "stop"
        if b["high"] >= target:
            return b["date"], target, "target"
    last = week_bars[-1]
    return last["date"], last["close"], "week_end"


def _manage_short(week_bars, start_idx, fill, stop, target):
    for b in week_bars[start_idx:]:
        if b["high"] >= stop:
            return b["date"], stop, "stop"
        if b["low"] <= target:
            return b["date"], target, "target"
    last = week_bars[-1]
    return last["date"], last["close"], "week_end"


def _r(direction, fill, stop, exit_price):
    risk = abs(fill - stop)
    move = (exit_price - fill) if direction == "long" else (fill - exit_price)
    return move / risk if risk else 0.0


def backtest_breakout(
    bars,
    min_history=60,
    stop_atr=STOP_ATR,
    target_atr=TARGET_ATR,
    vix_by_date=None,
    vix_max=None,
    include_shorts=True,
):
    """Weekly breakout with trend filter. One decision per week.

    include_shorts exists for research; the skill itself is long-only
    (shorts tested to negative expectancy 2010-2026).
    """
    weeks = weekly_groups(bars)
    trades = []
    n_hist = 0
    last_vix = None
    for i in range(len(weeks) - 1):
        n_hist += len(weeks[i])
        hist = bars[:n_hist]
        if vix_by_date is not None:
            last_vix = vix_by_date.get(hist[-1]["date"], last_vix)
        if n_hist < min_history:
            continue
        if vix_max is not None and (last_vix is None or last_vix > vix_max):
            continue
        trend = classify_trend([b["close"] for b in hist])
        a = atr(hist)
        if not a or trend == "range":
            continue
        week = weeks[i + 1]
        if trend == "uptrend":
            entry = max(b["high"] for b in weeks[i]) + BUFFER_ATR * a
            direction = "long"
        elif include_shorts:
            entry = min(b["low"] for b in weeks[i]) - BUFFER_ATR * a
            direction = "short"
        else:
            continue
        for j, b in enumerate(week):
            triggered = b["high"] >= entry if direction == "long" else b["low"] <= entry
            if not triggered:
                continue
            fill = max(entry, b["open"]) if direction == "long" else min(entry, b["open"])
            if direction == "long":
                stop, target = fill - stop_atr * a, fill + target_atr * a
                exit_date, exit_price, outcome = _manage_long(week, j, fill, stop, target)
            else:
                stop, target = fill + stop_atr * a, fill - target_atr * a
                exit_date, exit_price, outcome = _manage_short(week, j, fill, stop, target)
            trades.append(
                {
                    "setup": "weekly_breakout",
                    "direction": direction,
                    "entry_date": b["date"],
                    "fill": fill,
                    "stop": stop,
                    "target": target,
                    "exit_date": exit_date,
                    "exit": exit_price,
                    "outcome": outcome,
                    "r": _r(direction, fill, stop, exit_price),
                }
            )
            break
    return trades


def backtest_pullback(
    bars, min_history=60, stop_atr=STOP_ATR, target_atr=TARGET_ATR, vix_by_date=None, vix_max=None
):
    """Buy-limit at the 20d EMA in uptrends; long only."""
    weeks = weekly_groups(bars)
    trades = []
    n_hist = 0
    last_vix = None
    for i in range(len(weeks) - 1):
        n_hist += len(weeks[i])
        hist = bars[:n_hist]
        if vix_by_date is not None:
            last_vix = vix_by_date.get(hist[-1]["date"], last_vix)
        if n_hist < min_history:
            continue
        if vix_max is not None and (last_vix is None or last_vix > vix_max):
            continue
        closes = [b["close"] for b in hist]
        a = atr(hist)
        e20 = ema(closes, 20)
        if not a or not e20 or classify_trend(closes) != "uptrend" or e20 >= closes[-1]:
            continue
        week = weeks[i + 1]
        for j, b in enumerate(week):
            if b["low"] > e20:
                continue
            fill = min(e20, b["open"])
            stop, target = fill - stop_atr * a, fill + target_atr * a
            exit_date, exit_price, outcome = _manage_long(week, j, fill, stop, target)
            trades.append(
                {
                    "setup": "pullback_continuation",
                    "direction": "long",
                    "entry_date": b["date"],
                    "fill": fill,
                    "stop": stop,
                    "target": target,
                    "exit_date": exit_date,
                    "exit": exit_price,
                    "outcome": outcome,
                    "r": _r("long", fill, stop, exit_price),
                }
            )
            break
    return trades


def backtest_put_spread(bars, vix_by_date, min_history=60, step=5, dte=5):
    """Sell a 1-week put credit spread at -1 expected move in uptrends.

    Priced with Black-Scholes at entry (IV = vol index), settled at
    intrinsic value on the next week's final close. R = pnl / max_loss.
    """
    weeks = weekly_groups(bars)
    trades = []
    n_hist = 0
    last_vix = None
    for i in range(len(weeks) - 1):
        n_hist += len(weeks[i])
        hist = bars[:n_hist]
        decision = hist[-1]
        last_vix = vix_by_date.get(decision["date"], last_vix)
        if n_hist < min_history or last_vix is None or last_vix > VIX_STRESSED:
            continue
        if classify_trend([b["close"] for b in hist]) not in ("uptrend", "range"):
            continue
        spot = decision["close"]
        em = expected_move(spot, last_vix, dte)
        short_k = _round_strike(spot - EM_SHORT * em, step)
        wing = max(_round_strike(EM_WING * em, step), step)
        credit = _spread_credit(spot, short_k, short_k - wing, last_vix, dte, "put")
        if credit <= 0:
            continue
        settle_close = weeks[i + 1][-1]["close"]
        settle_value = max(short_k - settle_close, 0) - max(short_k - wing - settle_close, 0)
        pnl = credit - settle_value
        max_loss = wing - credit
        trades.append(
            {
                "setup": "put_credit_spread",
                "entry_date": decision["date"],
                "exit_date": weeks[i + 1][-1]["date"],
                "short_strike": short_k,
                "long_strike": short_k - wing,
                "credit": credit,
                "settle": settle_close,
                "pnl_points": pnl,
                "outcome": "expired_worthless" if settle_value == 0 else "tested",
                "r": pnl / max_loss if max_loss > 0 else 0.0,
            }
        )
    return trades


def backtest_monthly_spread(
    bars,
    vix_by_date,
    step=5,
    width_em=1.0,
    gated=True,
    min_history=262,
    take_profit=None,
    stop_mult=None,
    redeploy=False,
):
    """Monthly ATM bull put spread: sell the ATM put at month-end, buy the wing
    one 30d expected move lower, settle at the next month-end close.

    gated=True skips months where the core trend state is FLAT (below the
    hysteresis channel floor or the crash brake). Management (daily BS
    mark-to-market): take_profit=0.5 buys back at 50% of credit; stop_mult=2.0
    buys back when the spread marks at 2x credit (stand down for the month);
    redeploy=True re-enters a fresh ATM spread after a profit-take if >=7 days
    remain. Returns one record per month; pnl in points, caller converts via
    point value.
    """
    import datetime as _dt

    from signal_engine import CORE_BAND, CORE_BRAKE, CORE_HI_N, bs_price

    ends = [i for i in range(len(bars) - 1) if bars[i]["date"][:7] != bars[i + 1]["date"][:7]]
    trades = []
    lv = None
    for k in range(len(ends) - 1):
        i, j = ends[k], ends[k + 1]
        closes = [b["close"] for b in bars[: i + 1]]
        lv = vix_by_date.get(bars[i]["date"], lv)
        if len(closes) < min_history or lv is None:
            continue
        if gated:
            s200 = sma(closes, CORE_TREND_N)
            if not s200 or closes[-1] < max(
                s200 * CORE_BAND, max(closes[-CORE_HI_N:]) * CORE_BRAKE
            ):
                continue
        expiry = _dt.date.fromisoformat(bars[j]["date"])
        month_pnl, m = 0.0, i
        first = None
        outcome = "expired_worthless"
        entries = 0
        while m < j:
            spot = bars[m]["close"]
            dte = (expiry - _dt.date.fromisoformat(bars[m]["date"])).days
            if dte <= 0:
                break
            v0 = vix_by_date.get(bars[m]["date"], lv)
            lv = v0
            short_k = _round_strike(spot, step)
            wing = max(_round_strike(width_em * expected_move(spot, v0, dte), step), step)
            credit = bs_price(spot, short_k, dte / 365, v0 / 100, kind="put") - bs_price(
                spot, short_k - wing, dte / 365, v0 / 100, kind="put"
            )
            if credit <= 0:
                break
            entries += 1
            if first is None:
                first = {
                    "short_strike": short_k,
                    "long_strike": short_k - wing,
                    "credit": credit,
                    "max_loss": wing - credit,
                }
            exited = False
            for n in range(m + 1, j + 1):
                d = _dt.date.fromisoformat(bars[n]["date"])
                t_rem = max((expiry - d).days, 0) / 365
                vv = vix_by_date.get(bars[n]["date"], v0) / 100
                s = bars[n]["close"]
                if n == j or t_rem == 0:
                    payout = max(short_k - s, 0) - max(short_k - wing - s, 0)
                    month_pnl += credit - payout
                    if payout > 0:
                        outcome = "tested"
                    m = j
                    exited = True
                    break
                val = bs_price(s, short_k, t_rem, vv, kind="put") - bs_price(
                    s, short_k - wing, t_rem, vv, kind="put"
                )
                if take_profit and val <= (1 - take_profit) * credit:
                    month_pnl += credit - val
                    m = n if (redeploy and (expiry - d).days >= 7) else j
                    exited = True
                    break
                if stop_mult and val >= stop_mult * credit:
                    month_pnl += credit - val
                    outcome = "stopped"
                    m = j  # stand down for the rest of the month
                    exited = True
                    break
            if not exited:
                break
        if first is None:
            continue
        trades.append(
            {
                "setup": "monthly_bull_put_spread",
                "entry_date": bars[i]["date"],
                "exit_date": bars[j]["date"],
                "short_strike": first["short_strike"],
                "long_strike": first["long_strike"],
                "credit": first["credit"],
                "settle": bars[j]["close"],
                "entries": entries,
                "pnl_points": month_pnl,
                "max_loss": first["max_loss"],
                "outcome": outcome,
                "r": month_pnl / first["max_loss"] if first["max_loss"] > 0 else 0.0,
            }
        )
    return trades


def backtest_core_position(bars, lev=1.0, warmup=None, band=None, brake=None):
    """Regime-gated hold: long above the 200d SMA with a hysteresis channel
    (once long, exit only below band × SMA) and a crash circuit breaker
    (flat below brake × 52-week high).

    Weekly decision applied the following week; `lev` scales daily returns
    (futures leverage). Returns equity stats vs buy & hold over the same window.
    """
    from signal_engine import CORE_BAND, CORE_BRAKE, CORE_HI_N

    band = CORE_BAND if band is None else band
    brake = CORE_BRAKE if brake is None else brake
    warmup = warmup or max(CORE_TREND_N, CORE_HI_N) + 10
    weeks = weekly_groups(bars)
    eq, bh_eq, in_mkt, n = 1.0, 1.0, False, 0
    peak = dd = bh_peak = bh_dd = 0.0
    days = days_in = flips = 0
    last_state = None
    for i, w in enumerate(weeks):
        for j, b in enumerate(w):
            prev = (
                weeks[i - 1][-1]["close"]
                if j == 0 and i > 0
                else (w[j - 1]["close"] if j else None)
            )
            if prev and n >= warmup:
                days += 1
                r = b["close"] / prev - 1
                bh_eq *= 1 + r
                if in_mkt:
                    eq *= 1 + lev * r
                    days_in += 1
                peak = max(peak, eq)
                dd = min(dd, eq / peak - 1)
                bh_peak = max(bh_peak, bh_eq)
                bh_dd = min(bh_dd, bh_eq / bh_peak - 1)
        n += len(w)
        if n < warmup:
            continue
        closes = [x["close"] for x in bars[:n]]
        s200 = sma(closes, CORE_TREND_N)
        c = closes[-1]
        if not s200 or c < max(closes[-CORE_HI_N:]) * brake:
            state = False  # crash brake: fast waterfall exit
        elif in_mkt:
            state = c > s200 * band  # hysteresis channel while long
        else:
            state = c > s200  # fresh entries need a close above the SMA itself
        if last_state is not None and state != last_state:
            flips += 1
        last_state = in_mkt = state
    yrs = days / 252 if days else 1
    return {
        "leverage": lev,
        "cagr": round(eq ** (1 / yrs) - 1, 4),
        "max_dd": round(dd, 4),
        "final_multiple": round(eq, 2),
        "time_in_market": round(days_in / max(days, 1), 3),
        "regime_flips": flips,
        "buy_hold_cagr": round(bh_eq ** (1 / yrs) - 1, 4),
        "buy_hold_max_dd": round(bh_dd, 4),
        "years": round(yrs, 1),
    }


def summarize(trades):
    if not trades:
        return {"trades": 0}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "trades": len(rs),
        "win_rate": round(len(wins) / len(rs), 4),
        "avg_r": round(sum(rs) / len(rs), 4),
        "total_r": round(sum(rs), 2),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None,
        "max_drawdown_r": round(max_dd, 2),
        "best_r": round(max(rs), 2),
        "worst_r": round(min(rs), 2),
    }


# --- CLI (network) ---


def fetch_bars(yahoo_symbol, start):
    import yfinance as yf

    df = yf.Ticker(yahoo_symbol).history(start=start, interval="1d", auto_adjust=False)
    if df.empty:
        raise SystemExit(f"No data returned for {yahoo_symbol}")
    return [
        {
            "date": idx.date().isoformat(),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        }
        for idx, row in df.iterrows()
    ]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", nargs="+", default=["ES", "NQ"], choices=list(CONTRACTS))
    p.add_argument("--start", default="2010-01-01")
    p.add_argument("--output-dir", default="reports/index-futures-weekly/")
    args = p.parse_args()

    out = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "start": args.start,
        "results": {},
    }
    lines = [
        "# Index Futures Weekly Income — Backtest",
        "",
        f"**Period:** {args.start} → today • Conservative fills (stop-before-target on same bar)",
        "",
    ]
    for sym in args.symbols:
        c = CONTRACTS[sym]
        bars = fetch_bars(c["yahoo"], args.start)
        try:
            vix_bars = fetch_bars(c["vix_symbol"], args.start)
        except SystemExit:
            vix_bars = fetch_bars("^VIX", args.start)
        vix_by_date = {b["date"]: b["close"] for b in vix_bars}
        strategies = {
            "weekly_breakout_long": backtest_breakout(
                bars, vix_by_date=vix_by_date, vix_max=VIX_STRESSED, include_shorts=False
            ),
            "pullback_continuation": backtest_pullback(
                bars, vix_by_date=vix_by_date, vix_max=VIX_STRESSED
            ),
            "put_credit_spread": backtest_put_spread(bars, vix_by_date, step=c["strike_step"]),
        }
        out["results"][sym] = {name: summarize(t) for name, t in strategies.items()}
        out["results"][sym]["bars"] = len(bars)
        lines += [
            f"## {sym} ({c['index']}, {len(bars)} daily bars)",
            "",
            "| Strategy | Trades | Win rate | Avg R | Total R | Profit factor | Max DD (R) |",
            "|---|---|---|---|---|---|---|",
        ]
        for name, t in strategies.items():
            s = summarize(t)
            if s["trades"] == 0:
                lines.append(f"| {name} | 0 | – | – | – | – | – |")
                continue
            lines.append(
                f"| {name} | {s['trades']} | {s['win_rate']:.1%} | {s['avg_r']:.2f} "
                f"| {s['total_r']} | {s['profit_factor']} | {s['max_drawdown_r']} |"
            )
        lines += ["", "### Monthly ATM bull put spread (core-gated, per 1 spread)", ""]
        for label, kw in (
            ("hold-to-expiry", {}),
            (
                "managed (TP50 + redeploy + 2x stop)",
                {"take_profit": 0.5, "stop_mult": 2.0, "redeploy": True},
            ),
        ):
            m_trades = backtest_monthly_spread(bars, vix_by_date, step=c["strike_step"], **kw)
            m_pnls = [t["pnl_points"] * c["point_value"] for t in m_trades]
            if not m_pnls:
                continue
            m_wins = sum(1 for x in m_pnls if x > 0)
            m_losses = [x for x in m_pnls if x <= 0]
            lines.append(
                f"- {label}: {len(m_pnls)} months, **{m_wins} profitable "
                f"({m_wins / len(m_pnls):.0%})**, total **${sum(m_pnls):+,.0f}**, "
                f"avg losing month ${sum(m_losses) / len(m_losses):,.0f}, "
                f"worst ${min(m_pnls):,.0f}"
            )
            out["results"][sym][f"monthly_bull_put_spread_{label.split(' ')[0]}"] = {
                "months": len(m_pnls),
                "profitable": m_wins,
                "total_dollars_per_spread": round(sum(m_pnls)),
                "worst_month": round(min(m_pnls)),
            }
        lines += [
            "",
            "### Core trend position (hold > 200d SMA, weekly) vs buy & hold",
            "",
            "| Variant | CAGR | Max DD | $1 becomes | In market | Flips |",
            "|---|---|---|---|---|---|",
        ]
        for lev in (1.0, 1.5, 2.0):
            cp = backtest_core_position(bars, lev=lev)
            lines.append(
                f"| {lev}x | {cp['cagr']:+.1%} | {cp['max_dd']:.0%} | {cp['final_multiple']}x "
                f"| {cp['time_in_market']:.0%} | {cp['regime_flips']} |"
            )
            if lev == 1.0:
                lines.append(
                    f"| buy & hold | {cp['buy_hold_cagr']:+.1%} | {cp['buy_hold_max_dd']:.0%} "
                    f"| – | 100% | 0 |"
                )
        out["results"][sym]["core_position"] = {
            str(lev): backtest_core_position(bars, lev=lev) for lev in (1.0, 1.5, 2.0)
        }
        lines.append("")

    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    (outdir / f"index_futures_backtest_{stamp}.json").write_text(json.dumps(out, indent=2))
    (outdir / f"index_futures_backtest_{stamp}.md").write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"Saved to {outdir}/index_futures_backtest_{stamp}.{{md,json}}")


if __name__ == "__main__":
    main()
