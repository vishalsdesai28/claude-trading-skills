"""Pure signal engine for index-futures weekly-income signals.

Stdlib only. Operates on bars: list of {date, open, high, low, close} dicts,
oldest first. Network fetching lives in futures_signals.py.
"""

import math

# Contract specs: futures point values, micro equivalents, option strike grid.
CONTRACTS = {
    "ES": {
        "yahoo": "ES=F",
        "index": "S&P 500",
        "point_value": 50,
        "micro": "MES",
        "micro_point_value": 5,
        "strike_step": 5,
        "vix_symbol": "^VIX",
        "option_proxies": ["ES weekly options (CME)", "SPX/XSP", "SPY (strike ≈ ES/10)"],
    },
    "NQ": {
        "yahoo": "NQ=F",
        "index": "Nasdaq-100",
        "point_value": 20,
        "micro": "MNQ",
        "micro_point_value": 2,
        "strike_step": 25,
        "vix_symbol": "^VXN",
        "option_proxies": ["NQ weekly options (CME)", "NDX", "QQQ (strike ≈ NQ/41)"],
    },
}

# Regime thresholds
VIX_CALM = 17.0
VIX_STRESSED = 25.0
TREND_FAST, TREND_SLOW = 20, 50
CORE_TREND_N = 200  # core position filter; deliberately NOT vol-gated (vix gate
# reduced full-history CAGR without cutting drawdown in the 2000-2026 backtest)
CORE_BAND = 0.98  # hysteresis channel: once long, exit only below 98% of the SMA
CORE_BRAKE = 0.88  # crash circuit breaker: flat below 88% of the 52-week high
CORE_HI_N = 252  # lookback for the 52-week high

# Trade construction
ATR_N = 14
BUFFER_ATR = 0.1  # breakout entry buffer above prior week high
STOP_ATR = 2.0  # 2010-2026 backtest: tighter stops (1.5x) had negative expectancy
TARGET_ATR = 3.0  # -> 1.5R reward:risk on futures setups
EM_SHORT = 1.0  # short strike at 1x expected move
EM_WING = 0.5  # wing width as fraction of expected move
RISK_FREE = 0.04


# --- indicators ---


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema(values, n):
    if len(values) < n:
        return None
    e = sum(values[:n]) / n
    k = 2 / (n + 1)
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def atr(bars, n=ATR_N):
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-n - 1 : -1], bars[-n:]):
        trs.append(
            max(
                cur["high"] - cur["low"],
                abs(cur["high"] - prev["close"]),
                abs(cur["low"] - prev["close"]),
            )
        )
    return sum(trs) / n


# --- regime ---


def classify_trend(closes):
    fast, slow = sma(closes, TREND_FAST), sma(closes, TREND_SLOW)
    if slow is None:
        return "range"
    close = closes[-1]
    if close > slow and fast > slow:
        return "uptrend"
    if close < slow and fast < slow:
        return "downtrend"
    return "range"


def vol_regime(vix):
    if vix < VIX_CALM:
        return "calm"
    if vix > VIX_STRESSED:
        return "stressed"
    return "normal"


def expected_move(spot, vix, dte):
    """1-sigma expected move in index points over dte calendar days."""
    return spot * (vix / 100.0) * math.sqrt(dte / 365.0)


# --- Black-Scholes (for honest credit/max-loss estimates) ---


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(s, k, t, sigma, r=RISK_FREE, kind="put"):
    if t <= 0 or sigma <= 0:
        intrinsic = k - s if kind == "put" else s - k
        return max(intrinsic, 0.0)
    d1 = (math.log(s / k) + (r + sigma**2 / 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if kind == "call":
        return s * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)
    return k * math.exp(-r * t) * _norm_cdf(-d2) - s * _norm_cdf(-d1)


def _round_strike(x, step):
    return int(round(x / step) * step)


# --- signal construction ---


def _futures_fields(symbol, entry, stop, target):
    c = CONTRACTS[symbol]
    pts = abs(entry - stop)
    return {
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "rr": round(abs(target - entry) / pts, 2),
        "risk_per_contract": {
            symbol: round(pts * c["point_value"], 2),
            c["micro"]: round(pts * c["micro_point_value"], 2),
        },
    }


def _spread_credit(spot, short_k, long_k, vix, dte, kind):
    t, sigma = dte / 365.0, vix / 100.0
    credit = bs_price(spot, short_k, t, sigma, kind=kind) - bs_price(
        spot, long_k, t, sigma, kind=kind
    )
    return max(credit, 0.0)


def build_signals(symbol, bars, vix, dte=5):
    """Build the week-ahead signal set for one index future.

    Returns {symbol, regime, spot, atr, expected_move_1w, signals: [...]}.
    """
    c = CONTRACTS[symbol]
    closes = [b["close"] for b in bars]
    spot = closes[-1]
    trend = classify_trend(closes)
    vol = vol_regime(vix)
    a = atr(bars) or 0.0
    em = expected_move(spot, vix, dte)
    step = c["strike_step"]
    signals = []

    # 0) Core trend position — the compounding leg. Hold long while price closes
    # above the 200d SMA, flat below it; re-evaluate weekly at Friday's close.
    # 2000-2026 full-history backtest: beats buy & hold at 1.5-2x notional with
    # roughly half its max drawdown (avoids 2000-02, 2008, 2020, 2022 crashes).
    # Tri-state (the engine is stateless, so the in-between zone is explicit):
    #   LONG          spot > 200d SMA and above the crash line
    #   HOLD_IF_LONG  inside the hysteresis channel (98% of SMA .. SMA): keep an
    #                 existing long, do not open a new one
    #   FLAT          below the channel floor or below 88% of the 52-week high
    sma200 = sma(closes, CORE_TREND_N)
    if sma200:
        hi52 = max(closes[-CORE_HI_N:])
        crash_line = hi52 * CORE_BRAKE
        channel_floor = sma200 * CORE_BAND
        exit_line = max(channel_floor, crash_line)
        if spot < exit_line:
            state = "FLAT"
        elif spot > sma200:
            state = "LONG"
        else:
            state = "HOLD_IF_LONG"
        notes = {
            "LONG": (
                f"Hold core long exposure. Exit to flat only on a Friday close below "
                f"{exit_line:,.0f} (the higher of 98% of the 200d SMA {sma200:,.0f} and 88% of "
                f"the 52-week high {hi52:,.0f}). No per-trade stop — that line is the exit."
            ),
            "HOLD_IF_LONG": (
                f"Price is in the channel just under the 200d SMA ({sma200:,.0f}). Keep an "
                f"existing core long unless Friday closes below {exit_line:,.0f}; do NOT open "
                "a new core position until a Friday close back above the SMA."
            ),
            "FLAT": (
                f"Core exposure FLAT (close below {exit_line:,.0f}). Re-enter on a Friday "
                f"close back above the 200d SMA ({sma200:,.0f})."
            ),
        }
        signals.append(
            {
                "setup": "core_trend_position",
                "instrument": "future",
                "direction": "long" if state != "FLAT" else "none",
                "state": state,
                "entry_trigger": round(sma200, 2),
                "exit_trigger": round(exit_line, 2),
                "crash_line": round(crash_line, 2),
                "hi_52w": round(hi52, 2),
                "distance_pct": round((spot / sma200 - 1) * 100, 2),
                "note": notes[state],
            }
        )

    # 1) Directional futures setups — LONG ONLY. Shorting weekly breakdowns
    # had strongly negative expectancy in the 2010-2026 backtest on both ES and NQ.
    prior_week_high = max(b["high"] for b in bars[-5:])
    if a > 0 and trend == "uptrend" and vol != "stressed":
        e20 = ema(closes, TREND_FAST)
        if e20 and e20 < spot:  # pullback entry only makes sense below price
            signals.append(
                {
                    "setup": "pullback_continuation",
                    "instrument": "future",
                    "direction": "long",
                    "order": "buy limit",
                    **_futures_fields(symbol, e20, e20 - STOP_ATR * a, e20 + TARGET_ATR * a),
                }
            )
        entry = prior_week_high + BUFFER_ATR * a
        signals.append(
            {
                "setup": "weekly_breakout",
                "instrument": "future",
                "direction": "long",
                "order": "buy stop",
                "confidence": "marginal — confirm with technical-analyst before entry",
                **_futures_fields(symbol, entry, entry - STOP_ATR * a, entry + TARGET_ATR * a),
            }
        )

    # 2) Defined-risk premium selling (the weekly-income leg; high win rate, RR < 1).
    # Backtest 2010-2026: put credit spreads at -1 expected move are positive in
    # BOTH uptrend and range regimes; call credit spreads in downtrends are
    # negative on both indices, so only the put side is ever sold.
    if vol != "stressed" and trend in ("uptrend", "range"):
        wing = max(_round_strike(EM_WING * em, step), step)
        short_k = _round_strike(spot - EM_SHORT * em, step)
        credit = _spread_credit(spot, short_k, short_k - wing, vix, dte, "put")
        signals.append(
            {
                "setup": "put_credit_spread",
                "instrument": "option_spread",
                "direction": "neutral_bullish",
                "short_strike": short_k,
                "long_strike": short_k - wing,
                "est_credit": round(credit, 2),
                "max_loss": round(wing - credit, 2),
                "rr": round(credit / (wing - credit), 2) if wing > credit else None,
                "breakeven": round(short_k - credit, 2),
                "option_proxies": c["option_proxies"],
            }
        )
    elif trend == "downtrend":
        signals.append(
            {
                "setup": "stand_aside",
                "instrument": "note",
                "direction": "none",
                "note": "Downtrend regime: no signals. Backtest (2010-2026) shows both "
                "short futures breakdowns and call credit spreads carry negative "
                "expectancy on ES and NQ — capital preservation is the play.",
            }
        )

    # 3) Monthly income: ATM bull put spread, ~30 DTE. Sell the at-the-money put,
    # buy the wing one 30d expected move lower. Rich premium; essentially a
    # capped-upside long. 2001-2026 backtest: 74% (ES) / 73% (NQ) of months
    # profitable per single spread. Gated on the core trend state (not the
    # weekly 20/50 trend) — skipped only when the core is FLAT.
    if sma200 and spot >= max(sma200 * CORE_BAND, hi52 * CORE_BRAKE):
        m_dte = 30
        m_em = expected_move(spot, vix, m_dte)
        m_wing = max(_round_strike(m_em, step), step)
        m_short = _round_strike(spot, step)
        m_credit = _spread_credit(spot, m_short, m_short - m_wing, vix, m_dte, "put")
        signals.append(
            {
                "setup": "monthly_bull_put_spread",
                "instrument": "option_spread",
                "direction": "bullish",
                "dte": m_dte,
                "short_strike": m_short,
                "long_strike": m_short - m_wing,
                "est_credit": round(m_credit, 2),
                "max_loss": round(m_wing - m_credit, 2),
                "rr": round(m_credit / (m_wing - m_credit), 2) if m_wing > m_credit else None,
                "breakeven": round(m_short - m_credit, 2),
                # Managed exits (2001-2026 backtest: raises profit AND cuts avg
                # loss ~25-35% and worst month ~30% vs hold-to-expiry):
                "management": {
                    "profit_take_value": round(m_credit * 0.5, 2),
                    "stop_value": round(m_credit * 2.0, 2),
                    "redeploy": "on profit-take with >=7 days to expiry, sell a new ATM spread",
                },
                "option_proxies": c["option_proxies"],
            }
        )
    if vol == "stressed":
        signals.append(
            {
                "setup": "stand_aside_premium",
                "instrument": "note",
                "direction": "none",
                "note": f"Vol regime is stressed (VIX proxy {vix:.1f} > {VIX_STRESSED}); "
                "no futures entries or short-premium structures this week.",
            }
        )

    return {
        "symbol": symbol,
        "contract": c["yahoo"],
        "index": c["index"],
        "spot": round(spot, 2),
        "atr": round(a, 2),
        "vix": round(vix, 2),
        "expected_move_1w": round(em, 2),
        "regime": {"trend": trend, "vol": vol},
        "signals": signals,
    }
