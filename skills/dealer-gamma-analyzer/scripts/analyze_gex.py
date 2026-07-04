"""Dealer options gamma (GEX) analyzer built from FREE CBOE delayed options data.

CBOE publishes a free, ~15-minute-delayed options JSON that already carries
per-contract gamma, delta, IV and open interest, so dealer gamma exposure is
DIRECTLY computable with no paid feed. The 15-minute delay is irrelevant here:
dealer gamma positioning is a structural/daily map (where price gets pinned vs
where it runs), not a tick signal.

For any equity or index underlying this module produces:
  - total GEX under two dealer-positioning conventions:
      * Convention A "dealers short calls / long puts" (SqueezeMetrics net):
        net_gex = call_gamma_$ - put_gamma_$  (signed; drives the regime)
      * Convention B "customer-net-long-everything" (dealers short both):
        gross_hedge = call_gamma_$ + put_gamma_$  (upper-bound hedging pressure)
  - dollar gamma per 1% move per contract = OI * gamma * spot^2 (multiplier 100
    and the 1% factor cancel, since 100 * gamma * spot * (spot * 0.01) = gamma * spot^2)
  - call wall (overhead gamma resistance) + put wall (gamma support), as explicit
    S/R price levels
  - gamma-flip strike separating the pin regime from the trend/squeeze regime
  - max pain (the expiry pin price)
  - regime classification: positive-gamma (pin / mean-revert / low realized vol)
    vs negative-gamma (amplify / trend / squeeze-prone) + the magnet strikes

PURE parse/compute functions import with the standard library only and are unit
tested against a saved CBOE JSON fixture. The single network call, fetch_cboe(),
lazily imports urllib so nothing here touches the network on import.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime

# OCC symbol: ROOT + YYMMDD + C/P + strike(8 digits, price * 1000)
_OCC = re.compile(r"^([A-Z^_.]{1,6})(\d{6})([CP])(\d{8})$")
_MM = 1e6  # dollars -> millions (contract multiplier 100 is folded into dollar_gamma_1pct)

# Index underlyings live under a CBOE "_" namespace and often have aliases.
_INDEX_TICKERS = {
    "SPX": "_SPX",
    "SP500": "_SPX",
    "SPXW": "_SPXW",
    "NDX": "_NDX",
    "NASDAQ100": "_NDX",
    "RUT": "_RUT",
    "RUSSELL2000": "_RUT",
    "VIX": "_VIX",
    "DJX": "_DJX",
    "DOW": "_DJX",
    "XSP": "_XSP",
    "OEX": "_OEX",
    "XEO": "_XEO",
}


def underlying_for(ticker: str) -> str:
    """Map a user ticker to the CBOE delayed-quotes symbol.

    Strips a leading ^ or $ (Yahoo/other feeds), upper-cases, and translates
    index aliases (SPX -> _SPX, SP500 -> _SPX, NDX -> _NDX, ...). Already-prefixed
    CBOE index symbols (_SPX) and plain equity tickers pass through unchanged.
    """
    t = ticker.strip().upper().lstrip("^$")
    if t.startswith("_"):
        return t
    return _INDEX_TICKERS.get(t, t)


@dataclass(frozen=True)
class OptRow:
    strike: float
    is_call: bool
    oi: float
    gamma: float
    delta: float
    iv: float
    expiry: str  # YYMMDD


@dataclass
class GexReport:
    ticker: str
    spot: float
    net_gex_mm: float  # Convention A, signed, $MM per 1% move
    gross_hedge_mm: float  # Convention B, $MM per 1% move
    call_gex_mm: float
    put_gex_mm: float
    regime: str
    call_wall: float | None
    put_wall: float | None
    gamma_flip: float | None
    max_pain: float | None
    magnets: list = field(default_factory=list)
    call_put_oi_ratio: float | None = None
    atm_iv_pct: float | None = None
    n_contracts: int = 0
    note: str = ""


def parse_occ(sym: str):
    """Parse an OCC option symbol into (root, YYMMDD, is_call, strike).

    Returns None for anything that does not match the OCC layout.
    """
    m = _OCC.match(sym or "")
    if not m:
        return None
    root, yymmdd, cp, strike8 = m.groups()
    return root, yymmdd, (cp == "C"), int(strike8) / 1000.0


def rows_from_cboe(payload: dict):
    """Extract (spot, [OptRow]) from a CBOE delayed_quotes/options JSON payload."""
    data = payload.get("data", {}) or {}
    spot = float(data.get("current_price") or data.get("close") or 0)
    rows: list[OptRow] = []
    for o in data.get("options") or []:
        parsed = parse_occ(o.get("option", ""))
        if not parsed:
            continue
        _root, yymmdd, is_call, strike = parsed
        rows.append(
            OptRow(
                strike=strike,
                is_call=is_call,
                oi=float(o.get("open_interest") or 0),
                gamma=float(o.get("gamma") or 0),
                delta=float(o.get("delta") or 0),
                iv=float(o.get("iv") or 0),
                expiry=yymmdd,
            )
        )
    return spot, rows


def dollar_gamma_1pct(gamma: float, oi: float, spot: float) -> float:
    """Dollar gamma exposure per 1% spot move for one strike's OI.

    100 (multiplier) * gamma * spot * (spot * 0.01) = gamma * oi * spot^2.
    """
    return oi * gamma * spot * spot


def aggregate_gex(rows: list[OptRow], spot: float) -> dict:
    """Aggregate signed dealer gamma per strike (Convention A: calls +, puts -).

    Returns per-strike call/put/net dollar gamma (raw dollars), plus totals and
    per-strike OI. Put gamma is stored as a positive magnitude in ``put_gex``.
    """
    call_gex: dict[float, float] = {}
    put_gex: dict[float, float] = {}
    net_by_strike: dict[float, float] = {}
    call_oi: dict[float, float] = {}
    put_oi: dict[float, float] = {}
    for r in rows:
        g = dollar_gamma_1pct(r.gamma, r.oi, spot)
        if r.is_call:
            call_gex[r.strike] = call_gex.get(r.strike, 0.0) + g
            call_oi[r.strike] = call_oi.get(r.strike, 0.0) + r.oi
            net_by_strike[r.strike] = net_by_strike.get(r.strike, 0.0) + g
        else:
            put_gex[r.strike] = put_gex.get(r.strike, 0.0) + g
            put_oi[r.strike] = put_oi.get(r.strike, 0.0) + r.oi
            net_by_strike[r.strike] = net_by_strike.get(r.strike, 0.0) - g
    call_total = sum(call_gex.values())
    put_total = sum(put_gex.values())
    return {
        "call_gex": call_gex,
        "put_gex": put_gex,
        "net_by_strike": net_by_strike,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "call_total": call_total,
        "put_total": put_total,
        "net_total": call_total - put_total,
        "gross_total": call_total + put_total,
    }


def find_call_wall(call_gex: dict, spot: float):
    """Strike at or above spot with the largest call gamma (overhead resistance)."""
    above = {k: v for k, v in call_gex.items() if k >= spot}
    if not above:
        return None
    return max(above, key=lambda k: above[k])


def find_put_wall(put_gex: dict, spot: float):
    """Strike at or below spot with the largest put gamma (downside support)."""
    below = {k: v for k, v in put_gex.items() if k <= spot}
    if not below:
        return None
    return max(below, key=lambda k: below[k])


def find_gamma_flip(net_by_strike: dict):
    """Strike where cumulative net gamma (ascending strikes) crosses zero.

    Strike-space proxy for the zero-gamma level: below it puts dominate
    (negative-gamma / squeeze-prone), above it calls dominate (positive-gamma /
    pin). Returns None when cumulative net never changes sign.
    """
    cum = 0.0
    for k in sorted(net_by_strike):
        prev = cum
        cum += net_by_strike[k]
        if prev < 0 <= cum or prev > 0 >= cum:
            return k
    return None


def compute_max_pain(rows: list[OptRow], nearest_expiry_only: bool = True):
    """Strike minimizing total in-the-money value paid to option holders (pin)."""
    if not rows:
        return None
    if nearest_expiry_only:
        exp = min(r.expiry for r in rows)
        rows = [r for r in rows if r.expiry == exp]
    strikes = sorted({r.strike for r in rows})
    if not strikes:
        return None
    best_k = None
    best_pain = None
    for settle in strikes:
        pain = 0.0
        for r in rows:
            if r.is_call and settle > r.strike:
                pain += r.oi * (settle - r.strike)
            elif (not r.is_call) and settle < r.strike:
                pain += r.oi * (r.strike - settle)
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best_k = settle
    return best_k


def atm_iv_pct(rows: list[OptRow], spot: float):
    """ATM implied vol (%) = mean call/put IV at the nearest-expiry strike closest to spot."""
    if not rows or spot <= 0:
        return None
    exp = min(r.expiry for r in rows)
    near = [r for r in rows if r.expiry == exp]
    if not near:
        return None
    atm_strike = min({r.strike for r in near}, key=lambda k: abs(k - spot))
    ivs = [r.iv for r in near if r.strike == atm_strike and r.iv > 0]
    if not ivs:
        return None
    return round(sum(ivs) / len(ivs) * 100, 2)


def oi_stats(rows: list[OptRow]):
    """(total_call_oi, total_put_oi, call_put_oi_ratio)."""
    call_oi = sum(r.oi for r in rows if r.is_call)
    put_oi = sum(r.oi for r in rows if not r.is_call)
    ratio = round(call_oi / put_oi, 2) if put_oi > 0 else None
    return call_oi, put_oi, ratio


def classify_regime(net_total: float) -> str:
    """positive_gamma (pin / mean-revert) when net GEX >= 0, else negative_gamma."""
    return "positive_gamma" if net_total >= 0 else "negative_gamma"


def _magnet_strikes(net_by_strike: dict, call_oi: dict, put_oi: dict, top_n: int):
    """Top strikes by |net gamma| — where dealer hedging concentrates (price magnets)."""
    ranked = sorted(net_by_strike, key=lambda k: abs(net_by_strike[k]), reverse=True)
    out = []
    for k in ranked[:top_n]:
        out.append(
            {
                "strike": k,
                "net_gex_mm": round(net_by_strike[k] / _MM, 3),
                "call_oi": int(call_oi.get(k, 0)),
                "put_oi": int(put_oi.get(k, 0)),
            }
        )
    return out


def analyze(payload: dict, ticker: str | None = None, top_n_magnets: int = 5) -> GexReport:
    """Build a full GexReport from a CBOE options JSON payload."""
    spot, rows = rows_from_cboe(payload)
    sym = ticker or (payload.get("data", {}) or {}).get("symbol") or "UNKNOWN"
    if spot <= 0 or not rows:
        return GexReport(
            ticker=sym,
            spot=spot,
            net_gex_mm=0.0,
            gross_hedge_mm=0.0,
            call_gex_mm=0.0,
            put_gex_mm=0.0,
            regime="unknown",
            call_wall=None,
            put_wall=None,
            gamma_flip=None,
            max_pain=None,
            n_contracts=len(rows),
            note="No spot price or no parseable contracts in payload.",
        )
    agg = aggregate_gex(rows, spot)
    call_wall = find_call_wall(agg["call_gex"], spot)
    put_wall = find_put_wall(agg["put_gex"], spot)
    flip = find_gamma_flip(agg["net_by_strike"])
    max_pain = compute_max_pain(rows, nearest_expiry_only=True)
    _c, _p, cp_ratio = oi_stats(rows)
    regime = classify_regime(agg["net_total"])

    if flip is None:
        flip_note = "no gamma-flip strike (net gamma keeps one sign across the chain)"
    elif spot >= flip:
        flip_note = f"spot {spot:g} ABOVE gamma flip {flip:g} -> pin / mean-revert zone"
    else:
        flip_note = f"spot {spot:g} BELOW gamma flip {flip:g} -> trend / squeeze-prone zone"

    return GexReport(
        ticker=sym,
        spot=spot,
        net_gex_mm=round(agg["net_total"] / _MM, 3),
        gross_hedge_mm=round(agg["gross_total"] / _MM, 3),
        call_gex_mm=round(agg["call_total"] / _MM, 3),
        put_gex_mm=round(agg["put_total"] / _MM, 3),
        regime=regime,
        call_wall=call_wall,
        put_wall=put_wall,
        gamma_flip=flip,
        max_pain=max_pain,
        magnets=_magnet_strikes(agg["net_by_strike"], agg["call_oi"], agg["put_oi"], top_n_magnets),
        call_put_oi_ratio=cp_ratio,
        atm_iv_pct=atm_iv_pct(rows, spot),
        n_contracts=len(rows),
        note=flip_note,
    )


def _pct_from_spot(level, spot: float):
    if level is None or spot <= 0:
        return None
    return round((level - spot) / spot * 100, 2)


def report_to_dict(rep: GexReport) -> dict:
    """Serialize a GexReport to a JSON-friendly dict with an explicit S/R map."""
    regime_label = {
        "positive_gamma": "Positive gamma (dealers long gamma): pin / mean-revert / low realized vol",
        "negative_gamma": "Negative gamma (dealers short gamma): amplify / trend / squeeze-prone",
        "unknown": "Unknown (insufficient data)",
    }[rep.regime]
    return {
        "schema_version": "1.0",
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ticker": rep.ticker,
        "spot": rep.spot,
        "regime": rep.regime,
        "regime_label": regime_label,
        "gex": {
            "net_gex_mm_per_1pct": rep.net_gex_mm,
            "net_convention": "A: dealers short calls / long puts (SqueezeMetrics net = calls +, puts -)",
            "gross_hedge_mm_per_1pct": rep.gross_hedge_mm,
            "gross_convention": "B: customer-net-long-everything (dealers short both) = upper-bound hedging",
            "call_gex_mm_per_1pct": rep.call_gex_mm,
            "put_gex_mm_per_1pct": rep.put_gex_mm,
            "units": "$ millions of dealer hedging per 1% move in spot",
        },
        "support_resistance": {
            "resistance_call_wall": rep.call_wall,
            "resistance_call_wall_pct_from_spot": _pct_from_spot(rep.call_wall, rep.spot),
            "spot": rep.spot,
            "support_put_wall": rep.put_wall,
            "support_put_wall_pct_from_spot": _pct_from_spot(rep.put_wall, rep.spot),
            "gamma_flip": rep.gamma_flip,
            "gamma_flip_pct_from_spot": _pct_from_spot(rep.gamma_flip, rep.spot),
            "max_pain": rep.max_pain,
            "max_pain_pct_from_spot": _pct_from_spot(rep.max_pain, rep.spot),
        },
        "magnet_strikes": rep.magnets,
        "risk_indicators": {
            "call_put_oi_ratio": rep.call_put_oi_ratio,
            "atm_iv_pct": rep.atm_iv_pct,
            "contracts_analyzed": rep.n_contracts,
        },
        "note": rep.note,
        "caveats": [
            "GEX assumes uniform dealer positioning; the true dealer book is unobservable.",
            "The gamma flip is a strike-space proxy for the zero-gamma spot level, not a fitted surface.",
            "CBOE data is ~15 min delayed; this is a structural/daily map, not a tick signal.",
            "Descriptive, not predictive: it maps hedging pressure, it does not forecast price.",
        ],
    }


def generate_markdown_report(rep: GexReport) -> str:
    """Render a GexReport to markdown with the walls as explicit S/R levels."""
    d = report_to_dict(rep)
    sr = d["support_resistance"]

    def lvl(name, level, pct):
        if level is None:
            return f"- **{name}:** n/a"
        arrow = f"({pct:+.2f}% from spot)" if pct is not None else ""
        return f"- **{name}:** ${level:,.2f} {arrow}".rstrip()

    regime_emoji = "PIN" if rep.regime == "positive_gamma" else "SQUEEZE-PRONE"
    lines = [
        f"# Dealer Gamma (GEX) Analysis — {rep.ticker}",
        f"**Generated:** {d['generated']}",
        f"**Spot:** ${rep.spot:,.2f}",
        f"**Regime:** {regime_emoji} — {d['regime_label']}",
        "",
        "## Total Dealer Gamma Exposure ($MM per 1% move)",
        f"- **Net GEX (Convention A — dealers short calls / long puts):** {rep.net_gex_mm:+,.3f}",
        f"- **Gross hedging (Convention B — customer-net-long / dealers short both):** {rep.gross_hedge_mm:,.3f}",
        f"- Call gamma: {rep.call_gex_mm:,.3f}   |   Put gamma: {rep.put_gex_mm:,.3f}",
        "",
        "## Support / Resistance Map (gamma walls as price levels)",
        lvl(
            "Resistance — Call Wall",
            sr["resistance_call_wall"],
            sr["resistance_call_wall_pct_from_spot"],
        ),
        lvl("Spot", sr["spot"], 0.0),
        lvl("Support — Put Wall", sr["support_put_wall"], sr["support_put_wall_pct_from_spot"]),
        lvl("Gamma Flip (regime divider)", sr["gamma_flip"], sr["gamma_flip_pct_from_spot"]),
        lvl("Max Pain (expiry pin)", sr["max_pain"], sr["max_pain_pct_from_spot"]),
        f"- *{rep.note}*",
        "",
        "## Magnet Strikes (largest |net gamma| — where price gets pulled)",
    ]
    if rep.magnets:
        lines.append("| Strike | Net GEX ($MM) | Call OI | Put OI |")
        lines.append("|---|---|---|---|")
        for m in rep.magnets:
            lines.append(
                f"| {m['strike']:g} | {m['net_gex_mm']:+.3f} | {m['call_oi']:,} | {m['put_oi']:,} |"
            )
    else:
        lines.append("- none")
    lines += [
        "",
        "## Risk Indicators",
        f"- Call/Put OI ratio: {rep.call_put_oi_ratio if rep.call_put_oi_ratio is not None else 'n/a'}",
        f"- ATM IV: {rep.atm_iv_pct if rep.atm_iv_pct is not None else 'n/a'}%",
        f"- Contracts analyzed: {rep.n_contracts}",
        "",
        "## Trading Implication",
        _implication(rep),
        "",
        "## Caveats",
    ]
    lines += [f"- {c}" for c in d["caveats"]]
    return "\n".join(lines) + "\n"


def _implication(rep: GexReport) -> str:
    if rep.regime == "positive_gamma":
        return (
            "Dealers are net LONG gamma: they sell rallies and buy dips, dampening realized "
            "volatility. Expect mean-reversion and pinning toward the high-gamma strikes / max "
            "pain into expiry. Fade extremes toward the walls; breakouts through the call wall "
            "need real flow to stick."
        )
    if rep.regime == "negative_gamma":
        return (
            "Dealers are net SHORT gamma: they buy rallies and sell dips, AMPLIFYING moves. "
            "Trends and squeezes extend; the put wall breaking can accelerate downside and a "
            "push through the call wall can fuel a gamma squeeze. Respect momentum; mean-reversion "
            "fades are dangerous here."
        )
    return "Insufficient data to classify the gamma regime."


def fetch_cboe(ticker: str, timeout: float = 12.0):
    """Fetch the CBOE free delayed-quotes options JSON for a mapped ticker.

    Network call — imported lazily so pure functions stay stdlib-only and offline.
    Returns the parsed payload dict, or None on any fetch/parse failure.
    """
    import ssl
    import urllib.request

    sym = underlying_for(ticker)
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
    ctx = ssl.create_default_context()  # verified TLS; do not fall back to unverified
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.load(resp)
    except Exception as e:
        print(f"Error: CBOE fetch failed for {sym}: {e}", file=sys.stderr)
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quantify dealer options gamma (GEX) from FREE CBOE delayed options data."
    )
    parser.add_argument(
        "ticker",
        help="Underlying ticker (equity like NVDA, or index alias like SPX/NDX/RUT/VIX).",
    )
    parser.add_argument(
        "--payload-json",
        help="Path to a saved CBOE options JSON (offline mode; skips the network fetch).",
    )
    parser.add_argument(
        "--top-magnets",
        type=int,
        default=5,
        help="Number of magnet strikes to list (default: 5).",
    )
    parser.add_argument(
        "--output-dir",
        default="reports/",
        help="Output directory for reports (default: reports/).",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.payload_json:
        try:
            with open(args.payload_json) as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error: could not read payload JSON: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        payload = fetch_cboe(args.ticker)
        if payload is None:
            print(
                "Error: no CBOE data. The symbol may be unlisted, or the feed is "
                "unavailable. Retry, or pass --payload-json with a saved payload.",
                file=sys.stderr,
            )
            sys.exit(1)

    rep = analyze(payload, ticker=underlying_for(args.ticker), top_n_magnets=args.top_magnets)
    if rep.regime == "unknown":
        print(f"Error: {rep.note}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    base = f"dealer_gex_{rep.ticker.lstrip('_')}_{stamp}"

    json_path = os.path.join(args.output_dir, f"{base}.json")
    with open(json_path, "w") as f:
        json.dump(report_to_dict(rep), f, indent=2)
    print(f"JSON report: {json_path}")

    md_path = os.path.join(args.output_dir, f"{base}.md")
    with open(md_path, "w") as f:
        f.write(generate_markdown_report(rep))
    print(f"Markdown report: {md_path}")

    print(
        f"\n{rep.ticker} spot ${rep.spot:,.2f} | regime {rep.regime} | "
        f"net GEX {rep.net_gex_mm:+.3f}$MM/1% | call wall {rep.call_wall} | "
        f"put wall {rep.put_wall} | flip {rep.gamma_flip} | max pain {rep.max_pain}"
    )


if __name__ == "__main__":
    main()
