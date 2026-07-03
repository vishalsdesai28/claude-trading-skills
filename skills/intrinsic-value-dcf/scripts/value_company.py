"""Triangulated intrinsic-value estimate for a US ticker.

Blends three valuation methods into a single fair-value estimate with upside/downside
versus the current price:

1. **DCF** - 5-year unlevered FCFF projection with growth fade, WACC from CAPM (live 10Y
   UST risk-free rate + after-tax cost of debt), dual terminal value (Gordon + exit
   multiple, take the midpoint), discounted to an implied share price.
2. **Relative** - peer-median P/E, EV/Revenue, EV/EBITDA applied to the target's fundamentals.
3. **SOTP** - optional sum-of-the-parts when distinct operating segments are supplied.

Also emits a fully-recalculated WACC x terminal-growth sensitivity grid (the DCF is re-run
per cell), Bull/Base/Bear scenarios, guardrail gates (reject WACC <= g, flag terminal-value
share of EV outside sane bounds), and sector method routing (banks -> P/TBV, REITs -> P/FFO,
SaaS -> EV/Revenue + Rule of 40).

Data comes from Financial Modeling Prep (env ``FMP_API_KEY`` or ``--api-key``) with a keyless
yfinance fallback. Network libraries are imported lazily inside the fetch functions so the
pure calculation and normalization functions import with only the standard library.

Research/educational output. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- Defaults & constants ---------------------------------------------------

PROJECTION_YEARS = 5
DEFAULT_ERP = 0.055
DEFAULT_RF = 0.045  # stale fallback 10Y UST
DEFAULT_TERMINAL_G = 0.025
DEFAULT_KD_LARGE = 0.055
DEFAULT_KD_MID = 0.07
DEFAULT_EXIT_MULTIPLE = 12.0  # EV/EBITDA
DEFAULT_NWC_INTENSITY = 0.10
TAX_FLOOR = 0.15
TAX_CAP = 0.30
TV_SHARE_LOW = 0.45
TV_SHARE_HIGH = 0.85
MID_MARKET_CAP = 2_000_000_000
LARGE_MARKET_CAP = 10_000_000_000

# Sector-default levered betas (see references/wacc_rates.md).
SECTOR_BETAS = {
    "utilities": 0.55,
    "consumer defensive": 0.70,
    "consumer staples": 0.70,
    "communication services": 0.85,
    "telecom": 0.85,
    "healthcare": 0.90,
    "real estate": 0.90,
    "industrials": 1.05,
    "financial services": 1.15,
    "financial": 1.15,
    "consumer cyclical": 1.20,
    "consumer discretionary": 1.20,
    "energy": 1.10,
    "technology": 1.15,
    "basic materials": 1.10,
}

# WACC sanity bands by sector (low, high).
SECTOR_WACC_BANDS = {
    "utilities": (0.05, 0.07),
    "consumer defensive": (0.07, 0.09),
    "consumer staples": (0.07, 0.09),
    "communication services": (0.07, 0.10),
    "telecom": (0.07, 0.09),
    "healthcare": (0.08, 0.10),
    "real estate": (0.06, 0.08),
    "industrials": (0.08, 0.11),
    "financial services": (0.09, 0.12),
    "consumer cyclical": (0.09, 0.11),
    "consumer discretionary": (0.09, 0.11),
    "energy": (0.08, 0.12),
    "technology": (0.08, 0.13),
    "basic materials": (0.08, 0.11),
}


# --- Data model -------------------------------------------------------------


@dataclass
class CompanyFinancials:
    """Normalized inputs required to value a company, source-agnostic."""

    ticker: str
    current_price: float
    shares_outstanding: float
    total_debt: float
    cash: float
    base_revenue: float
    ebit_margin: float
    da_pct: float
    capex_pct: float
    nwc_intensity: float
    tax_rate: float
    market_cap: float | None = None
    beta: float | None = None
    sector: str | None = None
    industry: str | None = None
    hist_revenue_cagr: float | None = None
    y1_growth: float | None = None
    eps_ttm: float | None = None
    revenue_ttm: float | None = None
    ebitda_ttm: float | None = None
    fcf_margin: float | None = None
    interest_expense: float | None = None
    minority_interest: float = 0.0
    preferred_stock: float = 0.0
    peer_multiples: list[dict] = field(default_factory=list)
    source: str = "fmp"

    def resolved_market_cap(self) -> float:
        if self.market_cap and self.market_cap > 0:
            return self.market_cap
        return self.current_price * self.shares_outstanding

    def net_debt(self) -> float:
        return self.total_debt - self.cash


# --- Small helpers -----------------------------------------------------------


def _median(values: list) -> float | None:
    """Median of the non-None, finite numeric values, or None if empty."""
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return statistics.median(clean)


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _first(rows: list[dict], *keys: str) -> float | None:
    """First present, non-None value among ``keys`` from the most-recent row."""
    if not rows:
        return None
    row = rows[0]
    for k in keys:
        if row.get(k) is not None:
            return float(row[k])
    return None


def clamp_tax_rate(rate: float | None) -> float:
    if rate is None:
        return TAX_FLOOR + 0.06  # ~21% US statutory-ish midpoint
    return max(TAX_FLOOR, min(TAX_CAP, rate))


def size_premium_for(market_cap: float | None) -> float:
    if not market_cap or market_cap >= LARGE_MARKET_CAP:
        return 0.0
    if market_cap >= MID_MARKET_CAP:
        return 0.0075
    if market_cap >= 500_000_000:
        return 0.02
    if market_cap >= 100_000_000:
        return 0.03
    return 0.04


def default_beta_for(sector: str | None) -> float:
    if not sector:
        return 1.0
    return SECTOR_BETAS.get(sector.strip().lower(), 1.0)


# --- Sector routing ----------------------------------------------------------


def route_sector_method(sector: str | None, industry: str | None) -> dict:
    """Route to the appropriate primary valuation method for the sector.

    Banks -> P/TBV, REITs -> P/FFO, SaaS -> EV/Revenue + Rule of 40. Mature cash-flow
    names keep the DCF as primary. Returns whether the DCF is appropriate, the primary
    method, the relative multiples to emphasize, and a note.
    """
    s = (sector or "").strip().lower()
    ind = (industry or "").strip().lower()

    is_bank = "bank" in ind or ("financial" in s and "bank" in ind) or "insurance" in ind
    is_reit = "reit" in ind or (s == "real estate")
    is_saas = (
        "software" in ind or "saas" in ind or "application" in ind or "internet" in ind
    ) and ("technology" in s or "communication" in s)

    if is_bank:
        return {
            "primary": "relative",
            "relative_multiples": ["P/TBV", "P/B"],
            "dcf_appropriate": False,
            "note": "Bank/insurer: FCFF DCF is not meaningful; value on P/TBV or an "
            "excess-return model. DCF output is suppressed.",
        }
    if is_reit:
        return {
            "primary": "relative",
            "relative_multiples": ["P/FFO", "P/AFFO", "NAV"],
            "dcf_appropriate": False,
            "note": "REIT: use NAV or P/FFO; project NOI, not FCFF. DCF output is suppressed.",
        }
    if is_saas:
        return {
            "primary": "relative",
            "relative_multiples": ["EV/Revenue", "Rule of 40"],
            "dcf_appropriate": True,
            "note": "High-growth software: EV/Revenue + Rule of 40 anchor the value; "
            "run the DCF with care and low weight.",
        }
    return {
        "primary": "dcf",
        "relative_multiples": ["P/E", "EV/EBITDA", "EV/Revenue"],
        "dcf_appropriate": True,
        "note": "Mature cash-flow profile: DCF is the primary method, cross-checked by peers.",
    }


def rule_of_40(revenue_growth_pct: float | None, fcf_margin_pct: float | None) -> dict:
    """Rule of 40 for SaaS: revenue growth% + FCF margin% >= 40 is healthy."""
    if revenue_growth_pct is None or fcf_margin_pct is None:
        return {"score": None, "classification": "insufficient-data"}
    score = revenue_growth_pct + fcf_margin_pct
    if score >= 50:
        cls = "top-quartile"
    elif score >= 40:
        cls = "healthy"
    elif score >= 30:
        cls = "below-median"
    else:
        cls = "bottom-quartile"
    return {"score": round(score, 1), "classification": cls}


# --- WACC (CAPM) -------------------------------------------------------------


def cost_of_equity(rf: float, beta: float, erp: float, size_premium: float = 0.0) -> float:
    """CAPM cost of equity: Ke = rf + beta * ERP + size premium."""
    return rf + beta * erp + size_premium


def after_tax_cost_of_debt(kd: float, tax_rate: float) -> float:
    """After-tax cost of debt: Kd * (1 - tax rate)."""
    return kd * (1 - tax_rate)


def compute_wacc(market_cap: float, total_debt: float, ke: float, kd_after_tax: float) -> dict:
    """Market-value-weighted WACC. E = market cap, D = total debt, V = E + D."""
    v = market_cap + total_debt
    if v <= 0:
        raise ValueError("market cap + total debt must be positive to compute WACC")
    e_w = market_cap / v
    d_w = total_debt / v
    wacc = e_w * ke + d_w * kd_after_tax
    return {
        "wacc": wacc,
        "cost_of_equity": ke,
        "after_tax_cost_of_debt": kd_after_tax,
        "equity_weight": e_w,
        "debt_weight": d_w,
    }


def derive_cost_of_debt(
    interest_expense: float | None, total_debt: float | None, market_cap: float | None
) -> float:
    """Pre-tax cost of debt = interest_expense / total_debt, else a size-based default."""
    kd = _safe_div(interest_expense, total_debt)
    if kd is not None and 0.001 < kd < 0.25:
        return kd
    if market_cap and market_cap >= LARGE_MARKET_CAP:
        return DEFAULT_KD_LARGE
    return DEFAULT_KD_MID


# --- DCF engine --------------------------------------------------------------


def build_growth_path(
    y1_growth: float, terminal_g: float, years: int = PROJECTION_YEARS
) -> list[float]:
    """Linearly fade the growth rate from Year 1 to the terminal rate by the final year."""
    if years <= 1:
        return [terminal_g]
    step = (y1_growth - terminal_g) / (years - 1)
    return [y1_growth - step * i for i in range(years)]


def project_fcff(
    base_revenue: float,
    growth_path: list[float],
    ebit_margin: float,
    tax_rate: float,
    da_pct: float,
    capex_pct: float,
    nwc_intensity: float,
) -> tuple[list[float], list[float]]:
    """Project revenue and unlevered FCFF over the explicit horizon.

    FCFF = EBIT*(1-tax) + D&A - CapEx - dNWC, where dNWC scales with incremental revenue.
    """
    revenues: list[float] = []
    fcff: list[float] = []
    prev = base_revenue
    for g in growth_path:
        rev = prev * (1 + g)
        ebit = rev * ebit_margin
        nopat = ebit * (1 - tax_rate)
        da = rev * da_pct
        capex = rev * capex_pct
        dnwc = (rev - prev) * nwc_intensity
        fcff.append(nopat + da - capex - dnwc)
        revenues.append(rev)
        prev = rev
    return revenues, fcff


def terminal_value_gordon(last_fcff: float, terminal_g: float, wacc: float) -> float:
    """Perpetuity-growth terminal value. Requires WACC > g."""
    if wacc <= terminal_g:
        raise ValueError("WACC must exceed terminal growth (Gordon formula diverges)")
    return last_fcff * (1 + terminal_g) / (wacc - terminal_g)


def terminal_value_exit(last_ebitda: float, exit_multiple: float) -> float:
    """Exit-multiple terminal value: final-year EBITDA x EV/EBITDA multiple."""
    return last_ebitda * exit_multiple


def discount_cash_flows(fcff: list[float], wacc: float) -> float:
    """Present value of the explicit FCFF stream (end-of-year discounting)."""
    return sum(f / ((1 + wacc) ** (i + 1)) for i, f in enumerate(fcff))


def discount_terminal(tv: float, wacc: float, n_years: int) -> float:
    """Present value of the terminal value discounted from the final projection year."""
    return tv / ((1 + wacc) ** n_years)


def run_dcf(
    base_revenue: float,
    growth_path: list[float],
    ebit_margin: float,
    tax_rate: float,
    da_pct: float,
    capex_pct: float,
    nwc_intensity: float,
    wacc: float,
    terminal_g: float,
    exit_multiple: float,
    cash: float,
    total_debt: float,
    shares: float,
    minority_interest: float = 0.0,
    preferred: float = 0.0,
) -> dict:
    """Run a full DCF and return the implied share price plus all intermediates.

    This is the reusable engine: the sensitivity grid and scenario analysis both call it
    with substituted assumptions so every cell/scenario is a genuine re-run, not an
    approximation. Raises ValueError if WACC <= terminal growth (the guardrail gate).
    """
    if shares <= 0:
        raise ValueError("shares outstanding must be positive")
    if wacc <= terminal_g:
        raise ValueError("WACC must exceed terminal growth")

    revenues, fcff = project_fcff(
        base_revenue, growth_path, ebit_margin, tax_rate, da_pct, capex_pct, nwc_intensity
    )
    last_rev = revenues[-1]
    last_ebitda = last_rev * ebit_margin + last_rev * da_pct

    tv_gordon = terminal_value_gordon(fcff[-1], terminal_g, wacc)
    tv_exit = terminal_value_exit(last_ebitda, exit_multiple)
    tv_base = 0.5 * (tv_gordon + tv_exit)

    pv_fcff = discount_cash_flows(fcff, wacc)
    pv_tv = discount_terminal(tv_base, wacc, len(fcff))
    ev = pv_fcff + pv_tv
    equity = ev + cash - total_debt - minority_interest - preferred
    implied_price = equity / shares
    tv_share = pv_tv / ev if ev else None

    return {
        "revenues": [round(r, 2) for r in revenues],
        "fcff": [round(f, 2) for f in fcff],
        "tv_gordon": round(tv_gordon, 2),
        "tv_exit": round(tv_exit, 2),
        "tv_base": round(tv_base, 2),
        "pv_fcff": round(pv_fcff, 2),
        "pv_tv": round(pv_tv, 2),
        "enterprise_value": round(ev, 2),
        "equity_value": round(equity, 2),
        "implied_price": round(implied_price, 2),
        "tv_share_of_ev": round(tv_share, 4) if tv_share is not None else None,
    }


def _dcf_args_from(fin: CompanyFinancials, growth_path: list[float], exit_multiple: float) -> dict:
    """Assemble the constant run_dcf kwargs from a CompanyFinancials (excludes wacc/g)."""
    return {
        "base_revenue": fin.base_revenue,
        "growth_path": growth_path,
        "ebit_margin": fin.ebit_margin,
        "tax_rate": fin.tax_rate,
        "da_pct": fin.da_pct,
        "capex_pct": fin.capex_pct,
        "nwc_intensity": fin.nwc_intensity,
        "exit_multiple": exit_multiple,
        "cash": fin.cash,
        "total_debt": fin.total_debt,
        "shares": fin.shares_outstanding,
        "minority_interest": fin.minority_interest,
        "preferred": fin.preferred_stock,
    }


# --- Sensitivity & scenarios -------------------------------------------------


def sensitivity_grid(
    base_args: dict,
    base_wacc: float,
    wacc_steps: tuple = (-0.01, -0.005, 0.0, 0.005, 0.01),
    g_values: tuple = (0.015, 0.02, 0.025, 0.03, 0.035),
) -> dict:
    """WACC x terminal-growth grid. Re-runs the full DCF per cell.

    Cells where WACC <= g violate the guardrail gate and are returned as None.
    """
    rows = []
    for dx in wacc_steps:
        w = base_wacc + dx
        prices = []
        for g in g_values:
            if w <= g:
                prices.append(None)
                continue
            res = run_dcf(**{**base_args, "wacc": w, "terminal_g": g})
            prices.append(res["implied_price"])
        rows.append({"wacc": round(w, 5), "prices": prices})
    return {"g_values": [round(g, 4) for g in g_values], "rows": rows}


def build_scenarios(base_args: dict, base_wacc: float, base_growth_path: list[float]) -> dict:
    """Bull/Base/Bear scenarios, each a full DCF re-run with shifted levers.

    Bull/Bear shift revenue growth +/-300bps, EBIT margin +/-200bps, WACC -/+100bps, and set
    terminal g to 3.0% / 1.5%; Base keeps the inputs at 2.5% terminal g.
    """
    specs = {
        "bull": {"growth": 0.03, "margin": 0.02, "wacc": -0.01, "g": 0.030},
        "base": {"growth": 0.0, "margin": 0.0, "wacc": 0.0, "g": 0.025},
        "bear": {"growth": -0.03, "margin": -0.02, "wacc": 0.01, "g": 0.015},
    }
    out = {}
    base_margin = base_args["ebit_margin"]
    for name, s in specs.items():
        gp = [max(-0.5, g + s["growth"]) for g in base_growth_path]
        margin = max(0.0, base_margin + s["margin"])
        wacc = base_wacc + s["wacc"]
        g = s["g"]
        if wacc <= g:
            wacc = g + 0.005  # keep the gate satisfied for extreme combinations
        args = {
            **base_args,
            "ebit_margin": margin,
            "growth_path": gp,
            "wacc": wacc,
            "terminal_g": g,
        }
        res = run_dcf(**args)
        out[name] = {
            "implied_price": res["implied_price"],
            "wacc": round(wacc, 5),
            "terminal_g": g,
            "ebit_margin": round(margin, 4),
            "tv_share_of_ev": res["tv_share_of_ev"],
        }
    return out


# --- Relative valuation & SOTP ----------------------------------------------


def peer_median_multiples(peers: list[dict]) -> dict:
    """Median P/E, EV/Revenue, EV/EBITDA across a list of peer multiple dicts."""
    return {
        "pe": _median([p.get("pe_fwd", p.get("pe")) for p in peers]),
        "ev_rev": _median([p.get("ev_rev") for p in peers]),
        "ev_ebitda": _median([p.get("ev_ebitda") for p in peers]),
        "peer_count": len(peers),
    }


def relative_valuation(
    median_pe: float | None,
    median_ev_rev: float | None,
    median_ev_ebitda: float | None,
    eps_ttm: float | None,
    revenue_ttm: float | None,
    ebitda_ttm: float | None,
    net_debt: float,
    shares: float,
) -> dict:
    """Implied prices from peer-median multiples; blended = median of the available ones.

    Skips a multiple when the peer median is missing or the target's base metric is
    non-positive (e.g. negative EPS drops P/E; negative EBITDA drops EV/EBITDA).
    """
    implied: dict[str, float | None] = {"pe": None, "ev_rev": None, "ev_ebitda": None}
    if median_pe and eps_ttm and eps_ttm > 0:
        implied["pe"] = round(median_pe * eps_ttm, 2)
    if median_ev_rev and revenue_ttm and revenue_ttm > 0:
        implied["ev_rev"] = round((median_ev_rev * revenue_ttm - net_debt) / shares, 2)
    if median_ev_ebitda and ebitda_ttm and ebitda_ttm > 0:
        implied["ev_ebitda"] = round((median_ev_ebitda * ebitda_ttm - net_debt) / shares, 2)

    available = [v for v in implied.values() if v is not None]
    blended = round(statistics.median(available), 2) if available else None
    return {"implied": implied, "implied_price": blended}


def sotp_valuation(
    segments: list[dict],
    net_debt: float,
    shares: float,
    corporate_ev_deduction: float = 0.0,
    minority_interest: float = 0.0,
    preferred: float = 0.0,
    current_price: float | None = None,
) -> dict:
    """Sum-of-the-parts valuation.

    Each segment supplies either (ebitda, ev_ebitda) or (revenue, ev_rev). Segment EVs are
    summed, corporate/unallocated EV is deducted, then the standard equity bridge applies.
    """
    total_seg_ev = 0.0
    seg_detail = []
    for seg in segments:
        if seg.get("ebitda") is not None and seg.get("ev_ebitda") is not None:
            ev = seg["ebitda"] * seg["ev_ebitda"]
            basis = "EV/EBITDA"
        elif seg.get("revenue") is not None and seg.get("ev_rev") is not None:
            ev = seg["revenue"] * seg["ev_rev"]
            basis = "EV/Revenue"
        else:
            continue
        total_seg_ev += ev
        seg_detail.append({"name": seg.get("name", "segment"), "ev": round(ev, 2), "basis": basis})

    ev = total_seg_ev - corporate_ev_deduction
    equity = ev + (-net_debt) - minority_interest - preferred
    implied_price = equity / shares if shares else None
    discount = None
    if implied_price and current_price:
        discount = round((implied_price - current_price) / implied_price, 4)
    return {
        "segments": seg_detail,
        "total_segment_ev": round(total_seg_ev, 2),
        "corporate_ev_deduction": round(corporate_ev_deduction, 2),
        "enterprise_value": round(ev, 2),
        "equity_value": round(equity, 2),
        "implied_price": round(implied_price, 2) if implied_price is not None else None,
        "conglomerate_discount": discount,
    }


# --- Blending & guardrails ---------------------------------------------------


def blend_fair_value(
    dcf_price: float | None,
    rel_price: float | None,
    sotp_price: float | None,
    current_price: float,
    weights: dict | None = None,
) -> dict:
    """Blend method prices into a fair value; renormalize weights over available methods."""
    if weights is None:
        weights = (
            {"dcf": 0.4, "rel": 0.3, "sotp": 0.3}
            if sotp_price is not None
            else {"dcf": 0.5, "rel": 0.5}
        )
    prices = {"dcf": dcf_price, "rel": rel_price, "sotp": sotp_price}
    active = {k: w for k, w in weights.items() if prices.get(k) is not None}
    total_w = sum(active.values())
    if not active or total_w == 0:
        return {"fair_value": None, "weights": {}, "upside_pct": None}
    norm = {k: w / total_w for k, w in active.items()}
    fair = sum(norm[k] * prices[k] for k in norm)
    upside = (fair - current_price) / current_price if current_price else None
    return {
        "fair_value": round(fair, 2),
        "weights": {k: round(v, 3) for k, v in norm.items()},
        "upside_pct": round(upside, 4) if upside is not None else None,
    }


def evaluate_guardrails(
    wacc: float,
    terminal_g: float,
    tv_share: float | None,
    sector: str | None,
) -> list[str]:
    """Return human-readable guardrail warnings (empty list = all clear)."""
    warnings: list[str] = []
    if wacc <= terminal_g:
        warnings.append(
            f"GATE: WACC ({wacc:.2%}) <= terminal growth ({terminal_g:.2%}); "
            "terminal growth capped below WACC."
        )
    if tv_share is not None:
        if tv_share > TV_SHARE_HIGH:
            warnings.append(
                f"Terminal value is {tv_share:.0%} of EV (> {TV_SHARE_HIGH:.0%}); the model is "
                "largely a multiple-expansion bet — weight sensitivity over the point estimate."
            )
        elif tv_share < TV_SHARE_LOW:
            warnings.append(
                f"Terminal value is only {tv_share:.0%} of EV (< {TV_SHARE_LOW:.0%}); terminal "
                "assumptions may be too conservative."
            )
    if sector:
        band = SECTOR_WACC_BANDS.get(sector.strip().lower())
        if band and not (band[0] <= wacc <= band[1]):
            warnings.append(
                f"WACC ({wacc:.2%}) is outside the {sector} sanity band "
                f"({band[0]:.1%}-{band[1]:.1%}); double-check beta, capital structure, Kd."
            )
    return warnings


# --- Normalization (FMP / yfinance shapes -> CompanyFinancials) --------------


def normalize_fmp(raw: dict, ticker: str) -> CompanyFinancials:
    """Build CompanyFinancials from FMP-shaped responses (stdlib only, network-free).

    ``raw`` keys (each a list of annual rows, most recent first, or a single dict for
    profile/quote): ``profile``, ``income``, ``balance``, ``cashflow``, ``ratios``,
    ``quote`` (optional), ``peers`` (optional list of multiple dicts),
    ``risk_free_rate`` (optional float).
    """
    profile = raw.get("profile") or [{}]
    profile = profile[0] if isinstance(profile, list) else profile
    income = raw.get("income") or []
    balance = raw.get("balance") or []
    cashflow = raw.get("cashflow") or []
    ratios = raw.get("ratios") or []
    quote = raw.get("quote") or []
    quote = (
        quote[0]
        if isinstance(quote, list) and quote
        else (quote if isinstance(quote, dict) else {})
    )

    price = profile.get("price") or quote.get("price")
    if price is None:
        raise ValueError(f"could not determine current price for {ticker}")
    price = float(price)

    shares = _first(income, "weightedAverageShsOutDil", "weightedAverageShsOut")
    if not shares:
        shares = profile.get("sharesOutstanding") or quote.get("sharesOutstanding")
    if not shares:
        raise ValueError(f"could not determine shares outstanding for {ticker}")
    shares = float(shares)

    market_cap = profile.get("mktCap") or profile.get("marketCap") or quote.get("marketCap")
    market_cap = float(market_cap) if market_cap else price * shares

    total_debt = _first(balance, "totalDebt") or 0.0
    cash = _first(balance, "cashAndShortTermInvestments", "cashAndCashEquivalents") or 0.0
    minority = _first(balance, "minorityInterest") or 0.0
    preferred = _first(balance, "preferredStock") or 0.0

    # Margins & intensities from up to 3 recent years (medians).
    n = min(3, len(income))
    ebit_margins = [
        _safe_div(income[i].get("operatingIncome"), income[i].get("revenue")) for i in range(n)
    ]
    da_pcts = []
    capex_pcts = []
    for i in range(min(3, len(cashflow))):
        rev_i = income[i].get("revenue") if i < len(income) else None
        da_pcts.append(_safe_div(cashflow[i].get("depreciationAndAmortization"), rev_i))
        capex = cashflow[i].get("capitalExpenditure")
        capex_pcts.append(_safe_div(abs(capex) if capex is not None else None, rev_i))

    ebit_margin = _median(ebit_margins) or 0.12
    da_pct = _median(da_pcts) or 0.04
    capex_pct = _median(capex_pcts) or 0.05

    # Effective tax rate from ratios or income statement, clamped.
    tax_candidates = [r.get("effectiveTaxRate") for r in ratios[:3]]
    if not any(t is not None for t in tax_candidates):
        tax_candidates = [
            _safe_div(income[i].get("incomeTaxExpense"), income[i].get("incomeBeforeTax"))
            for i in range(n)
        ]
    tax_rate = clamp_tax_rate(_median(tax_candidates))

    # Operating NWC intensity from the most recent balance sheet.
    cur_assets = _first(balance, "totalCurrentAssets")
    cur_liabs = _first(balance, "totalCurrentLiabilities")
    st_debt = _first(balance, "shortTermDebt") or 0.0
    base_revenue = _first(income, "revenue")
    if base_revenue is None or base_revenue <= 0:
        raise ValueError(f"could not determine revenue for {ticker}")
    base_revenue = float(base_revenue)
    if cur_assets is not None and cur_liabs is not None:
        nwc_level = (cur_assets - cash) - (cur_liabs - st_debt)
        nwc_intensity = max(0.0, nwc_level / base_revenue)
    else:
        nwc_intensity = DEFAULT_NWC_INTENSITY

    # Historical revenue CAGR (oldest -> newest available).
    revs = [income[i].get("revenue") for i in range(len(income)) if income[i].get("revenue")]
    hist_cagr = None
    if len(revs) >= 2 and revs[-1] and revs[-1] > 0:
        periods = len(revs) - 1
        hist_cagr = (revs[0] / revs[-1]) ** (1 / periods) - 1

    # TTM proxies (most recent annual).
    eps_ttm = _first(income, "epsdiluted", "eps")
    ebitda_ttm = _first(income, "ebitda")
    if ebitda_ttm is None:
        op_inc = _first(income, "operatingIncome")
        da = _first(cashflow, "depreciationAndAmortization")
        ebitda_ttm = (op_inc + da) if (op_inc is not None and da is not None) else None
    fcf_margin = None
    op_cf = _first(cashflow, "operatingCashFlow")
    capex0 = _first(cashflow, "capitalExpenditure")
    if op_cf is not None and capex0 is not None and base_revenue:
        fcf_margin = (op_cf - abs(capex0)) / base_revenue

    beta = profile.get("beta")
    beta = float(beta) if beta not in (None, 0) else default_beta_for(profile.get("sector"))
    interest_expense = _first(income, "interestExpense")

    return CompanyFinancials(
        ticker=ticker.upper(),
        current_price=price,
        shares_outstanding=shares,
        total_debt=total_debt,
        cash=cash,
        base_revenue=base_revenue,
        ebit_margin=ebit_margin,
        da_pct=da_pct,
        capex_pct=capex_pct,
        nwc_intensity=nwc_intensity,
        tax_rate=tax_rate,
        market_cap=market_cap,
        beta=beta,
        sector=profile.get("sector"),
        industry=profile.get("industry"),
        hist_revenue_cagr=hist_cagr,
        y1_growth=hist_cagr,
        eps_ttm=eps_ttm,
        revenue_ttm=base_revenue,
        ebitda_ttm=ebitda_ttm,
        fcf_margin=fcf_margin,
        interest_expense=interest_expense,
        minority_interest=minority,
        preferred_stock=preferred,
        peer_multiples=raw.get("peers") or [],
        source="fmp",
    )


# --- Orchestration -----------------------------------------------------------


def value_company(
    fin: CompanyFinancials,
    rf: float,
    erp: float = DEFAULT_ERP,
    terminal_g: float = DEFAULT_TERMINAL_G,
    exit_multiple: float = DEFAULT_EXIT_MULTIPLE,
    y1_growth: float | None = None,
    segments: list[dict] | None = None,
    corporate_ev_deduction: float = 0.0,
) -> dict:
    """Full triangulated valuation. Returns a JSON-serializable result dict."""
    routing = route_sector_method(fin.sector, fin.industry)

    # --- WACC ---
    beta = fin.beta if fin.beta else default_beta_for(fin.sector)
    size_prem = size_premium_for(fin.resolved_market_cap())
    ke = cost_of_equity(rf, beta, erp, size_prem)
    kd = derive_cost_of_debt(fin.interest_expense, fin.total_debt, fin.resolved_market_cap())
    kd_at = after_tax_cost_of_debt(kd, fin.tax_rate)
    wacc_info = compute_wacc(fin.resolved_market_cap(), fin.total_debt, ke, kd_at)
    wacc = wacc_info["wacc"]

    # --- Terminal-growth gate (cap g below WACC for the base case) ---
    effective_g = terminal_g
    if wacc <= effective_g:
        effective_g = max(0.0, wacc - 0.005)

    # --- Growth path ---
    y1 = (
        y1_growth
        if y1_growth is not None
        else (fin.y1_growth if fin.y1_growth is not None else 0.05)
    )
    growth_path = build_growth_path(y1, effective_g)

    # --- DCF ---
    dcf = None
    dcf_price = None
    sensitivity = None
    scenarios = None
    base_args = _dcf_args_from(fin, growth_path, exit_multiple)
    if routing["dcf_appropriate"]:
        dcf = run_dcf(**{**base_args, "wacc": wacc, "terminal_g": effective_g})
        dcf_price = dcf["implied_price"]
        sensitivity = sensitivity_grid(base_args, wacc)
        scenarios = build_scenarios(base_args, wacc, growth_path)

    # --- Relative ---
    peer_meds = peer_median_multiples(fin.peer_multiples) if fin.peer_multiples else {}
    rel = None
    rel_price = None
    if peer_meds and peer_meds.get("peer_count"):
        rel = relative_valuation(
            peer_meds.get("pe"),
            peer_meds.get("ev_rev"),
            peer_meds.get("ev_ebitda"),
            fin.eps_ttm,
            fin.revenue_ttm,
            fin.ebitda_ttm,
            fin.net_debt(),
            fin.shares_outstanding,
        )
        rel_price = rel["implied_price"]

    # --- SOTP (optional) ---
    sotp = None
    sotp_price = None
    if segments:
        sotp = sotp_valuation(
            segments,
            fin.net_debt(),
            fin.shares_outstanding,
            corporate_ev_deduction=corporate_ev_deduction,
            minority_interest=fin.minority_interest,
            preferred=fin.preferred_stock,
            current_price=fin.current_price,
        )
        sotp_price = sotp["implied_price"]

    # --- Blend ---
    blended = blend_fair_value(dcf_price, rel_price, sotp_price, fin.current_price)

    # --- Rule of 40 (SaaS) ---
    r40 = None
    if "Rule of 40" in routing.get("relative_multiples", []):
        growth_pct = (y1 * 100) if y1 is not None else None
        fcf_margin_pct = (fin.fcf_margin * 100) if fin.fcf_margin is not None else None
        r40 = rule_of_40(growth_pct, fcf_margin_pct)

    # --- Guardrails ---
    tv_share = dcf["tv_share_of_ev"] if dcf else None
    warnings = evaluate_guardrails(wacc, effective_g, tv_share, fin.sector)
    if effective_g != terminal_g:
        warnings.insert(
            0,
            f"GATE: requested terminal growth ({terminal_g:.2%}) >= WACC ({wacc:.2%}); "
            f"capped to {effective_g:.2%} to keep the Gordon formula valid.",
        )

    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ticker": fin.ticker,
        "disclaimer": "Research/educational output. Not financial advice.",
        "snapshot": {
            "current_price": fin.current_price,
            "market_cap": round(fin.resolved_market_cap(), 2),
            "sector": fin.sector,
            "industry": fin.industry,
            "net_debt": round(fin.net_debt(), 2),
            "shares_outstanding": fin.shares_outstanding,
            "source": fin.source,
        },
        "sector_routing": routing,
        "assumptions": {
            "risk_free_rate": round(rf, 5),
            "equity_risk_premium": round(erp, 5),
            "beta": round(beta, 3),
            "size_premium": round(size_prem, 5),
            "cost_of_equity": round(ke, 5),
            "pre_tax_cost_of_debt": round(kd, 5),
            "after_tax_cost_of_debt": round(kd_at, 5),
            "tax_rate": round(fin.tax_rate, 5),
            "wacc": round(wacc, 5),
            "equity_weight": round(wacc_info["equity_weight"], 4),
            "debt_weight": round(wacc_info["debt_weight"], 4),
            "y1_growth": round(y1, 5) if y1 is not None else None,
            "terminal_growth": round(effective_g, 5),
            "requested_terminal_growth": round(terminal_g, 5),
            "exit_multiple": exit_multiple,
            "growth_path": [round(g, 5) for g in growth_path],
            "ebit_margin": round(fin.ebit_margin, 5),
            "da_pct": round(fin.da_pct, 5),
            "capex_pct": round(fin.capex_pct, 5),
            "nwc_intensity": round(fin.nwc_intensity, 5),
        },
        "dcf": dcf,
        "relative": {"peer_medians": peer_meds, **(rel or {})} if rel else None,
        "sotp": sotp,
        "rule_of_40": r40,
        "blended": blended,
        "sensitivity": sensitivity,
        "scenarios": scenarios,
        "guardrail_warnings": warnings,
    }


# --- Fetch layer (lazy network imports; not exercised by tests) --------------


def _fmp_get(session, endpoint: str, api_key: str, params: dict | None = None) -> object:
    base = "https://financialmodelingprep.com/stable"
    params = dict(params or {})
    params["apikey"] = api_key
    resp = session.get(f"{base}/{endpoint}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_financials_fmp(ticker: str, api_key: str, peer_limit: int = 6) -> CompanyFinancials:
    """Fetch statements/ratios/profile/peers from FMP and normalize. Lazy-imports requests."""
    import requests  # noqa: PLC0415 - lazy so pure functions import without network deps

    session = requests.Session()
    sym = {"symbol": ticker, "period": "annual", "limit": 5}
    raw = {
        "profile": _fmp_get(session, "profile", api_key, {"symbol": ticker}),
        "income": _fmp_get(session, "income-statement", api_key, sym),
        "balance": _fmp_get(session, "balance-sheet-statement", api_key, sym),
        "cashflow": _fmp_get(session, "cash-flow-statement", api_key, sym),
        "ratios": _fmp_get(session, "ratios", api_key, sym),
    }
    # Peers: fetch each peer's key metrics to build multiples.
    peers_raw = _fmp_get(session, "stock-peers", api_key, {"symbol": ticker})
    peer_syms = [p.get("symbol") for p in (peers_raw or []) if isinstance(p, dict)][:peer_limit]
    peer_multiples = []
    for ps in peer_syms:
        try:
            km = _fmp_get(session, "key-metrics-ttm", api_key, {"symbol": ps})
            rt = _fmp_get(session, "ratios-ttm", api_key, {"symbol": ps})
            km0 = km[0] if isinstance(km, list) and km else {}
            rt0 = rt[0] if isinstance(rt, list) and rt else {}
            peer_multiples.append(
                {
                    "symbol": ps,
                    "pe_fwd": rt0.get("priceToEarningsRatioTTM") or rt0.get("peRatioTTM"),
                    "ev_rev": km0.get("evToSalesTTM") or km0.get("enterpriseValueOverRevenueTTM"),
                    "ev_ebitda": km0.get("evToEBITDATTM")
                    or km0.get("enterpriseValueOverEBITDATTM"),
                }
            )
        except Exception as exc:  # noqa: BLE001 - a bad peer must not sink the whole run
            print(f"WARN: peer {ps} multiples unavailable: {exc}", file=sys.stderr)
    raw["peers"] = peer_multiples
    return normalize_fmp(raw, ticker)


def fetch_risk_free_rate_fmp(api_key: str) -> float | None:
    """10Y UST from FMP treasury-rates (field year10), as a decimal. Lazy-imports requests."""
    import requests  # noqa: PLC0415

    try:
        session = requests.Session()
        data = _fmp_get(session, "treasury-rates", api_key)
        if isinstance(data, list) and data:
            y10 = data[0].get("year10")
            if y10 is not None:
                return float(y10) / 100.0
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: could not fetch live 10Y UST: {exc}", file=sys.stderr)
    return None


def fetch_financials_yfinance(ticker: str) -> CompanyFinancials:
    """Keyless fallback via yfinance -> FMP-compatible raw dict -> normalize. Lazy import."""
    import yfinance as yf  # noqa: PLC0415

    t = yf.Ticker(ticker)
    info = t.info or {}
    income = t.income_stmt
    cashflow = t.cashflow
    balance = t.balance_sheet

    def col(df, label, idx=0):
        try:
            if df is not None and label in df.index:
                return float(df.loc[label].iloc[idx])
        except Exception:  # noqa: BLE001
            return None
        return None

    n_cols = 0
    try:
        n_cols = income.shape[1] if income is not None else 0
    except Exception:  # noqa: BLE001
        n_cols = 0

    income_rows = []
    for i in range(min(3, n_cols)):
        income_rows.append(
            {
                "revenue": col(income, "Total Revenue", i),
                "operatingIncome": col(income, "Operating Income", i),
                "incomeBeforeTax": col(income, "Pretax Income", i),
                "incomeTaxExpense": col(income, "Tax Provision", i),
                "epsdiluted": info.get("trailingEps") if i == 0 else None,
                "weightedAverageShsOutDil": info.get("sharesOutstanding") if i == 0 else None,
                "interestExpense": col(income, "Interest Expense", i),
            }
        )
    cashflow_rows = [
        {
            "depreciationAndAmortization": col(cashflow, "Depreciation And Amortization"),
            "capitalExpenditure": col(cashflow, "Capital Expenditure"),
            "operatingCashFlow": col(cashflow, "Operating Cash Flow"),
        }
    ]
    balance_rows = [
        {
            "totalDebt": info.get("totalDebt"),
            "cashAndShortTermInvestments": info.get("totalCash"),
            "totalCurrentAssets": col(balance, "Current Assets"),
            "totalCurrentLiabilities": col(balance, "Current Liabilities"),
            "shortTermDebt": col(balance, "Current Debt"),
        }
    ]
    raw = {
        "profile": [
            {
                "price": info.get("currentPrice") or info.get("regularMarketPrice"),
                "beta": info.get("beta"),
                "mktCap": info.get("marketCap"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "sharesOutstanding": info.get("sharesOutstanding"),
            }
        ],
        "income": income_rows,
        "cashflow": cashflow_rows,
        "balance": balance_rows,
        "ratios": [],
        "peers": [],
    }
    fin = normalize_fmp(raw, ticker)
    fin.source = "yfinance"
    return fin


# --- Report rendering --------------------------------------------------------


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"${v:,.2f}"


def generate_markdown_report(result: dict) -> str:
    a = result["assumptions"]
    snap = result["snapshot"]
    blended = result["blended"]
    lines = [
        f"# Intrinsic Value — {result['ticker']}",
        f"*Generated {result['generated_at']}. {result['disclaimer']}*",
        "",
        "## Headline",
    ]
    if blended.get("fair_value") is not None:
        direction = "upside" if (blended.get("upside_pct") or 0) >= 0 else "downside"
        lines.append(
            f"Blended fair value {_fmt_money(blended['fair_value'])} vs current "
            f"{_fmt_money(snap['current_price'])} -> "
            f"{blended['upside_pct']:+.1%} {direction}."
        )
    else:
        lines.append("Insufficient data to compute a blended fair value.")
    lines += [
        "",
        "## Snapshot",
        f"- Sector / industry: {snap['sector']} / {snap['industry']}",
        f"- Market cap: {_fmt_money(snap['market_cap'])}",
        f"- Net debt: {_fmt_money(snap['net_debt'])}",
        f"- Data source: {snap['source']}",
        "",
        "## Sector Routing",
        f"- Primary method: **{result['sector_routing']['primary']}**",
        f"- Emphasized multiples: {', '.join(result['sector_routing']['relative_multiples'])}",
        f"- {result['sector_routing']['note']}",
        "",
        "## Method Summary",
        "| Method | Implied price | Weight |",
        "|---|---|---|",
    ]
    dcf_price = result["dcf"]["implied_price"] if result.get("dcf") else None
    rel_price = result["relative"]["implied_price"] if result.get("relative") else None
    sotp_price = result["sotp"]["implied_price"] if result.get("sotp") else None
    weights = blended.get("weights", {})
    lines.append(f"| DCF | {_fmt_money(dcf_price)} | {weights.get('dcf', 0):.0%} |")
    lines.append(f"| Relative | {_fmt_money(rel_price)} | {weights.get('rel', 0):.0%} |")
    lines.append(f"| SOTP | {_fmt_money(sotp_price)} | {weights.get('sotp', 0):.0%} |")
    lines.append(f"| **Blended** | **{_fmt_money(blended.get('fair_value'))}** | — |")
    lines.append("")

    lines += [
        "## WACC & Assumptions",
        f"- Risk-free (10Y UST): {a['risk_free_rate']:.2%}  |  ERP: {a['equity_risk_premium']:.2%}"
        f"  |  Beta: {a['beta']}",
        f"- Cost of equity: {a['cost_of_equity']:.2%}  |  After-tax cost of debt: "
        f"{a['after_tax_cost_of_debt']:.2%}  |  Tax: {a['tax_rate']:.1%}",
        f"- **WACC: {a['wacc']:.2%}** (E {a['equity_weight']:.0%} / D {a['debt_weight']:.0%})",
        f"- Terminal growth: {a['terminal_growth']:.2%}  |  Exit multiple: {a['exit_multiple']}x"
        f"  |  Y1 growth: {a['y1_growth']:.2%}"
        if a["y1_growth"] is not None
        else f"- Terminal growth: {a['terminal_growth']:.2%}",
        f"- Margins: EBIT {a['ebit_margin']:.1%}, D&A {a['da_pct']:.1%}, CapEx "
        f"{a['capex_pct']:.1%}, NWC intensity {a['nwc_intensity']:.1%}",
        "",
    ]

    if result.get("dcf"):
        dcf = result["dcf"]
        lines += [
            "## DCF Build",
            "| Year | Revenue | FCFF |",
            "|---|---|---|",
        ]
        for i, (rev, f) in enumerate(zip(dcf["revenues"], dcf["fcff"]), start=1):
            lines.append(f"| Y{i} | {_fmt_money(rev)} | {_fmt_money(f)} |")
        lines += [
            "",
            f"- PV of FCFF: {_fmt_money(dcf['pv_fcff'])}  |  PV of TV: {_fmt_money(dcf['pv_tv'])}",
            f"- Terminal value: Gordon {_fmt_money(dcf['tv_gordon'])} / exit "
            f"{_fmt_money(dcf['tv_exit'])} -> midpoint {_fmt_money(dcf['tv_base'])}",
            f"- Enterprise value: {_fmt_money(dcf['enterprise_value'])}  ->  equity "
            f"{_fmt_money(dcf['equity_value'])}",
            f"- **Implied DCF price: {_fmt_money(dcf['implied_price'])}** "
            f"(TV = {dcf['tv_share_of_ev']:.0%} of EV)"
            if dcf["tv_share_of_ev"] is not None
            else f"- **Implied DCF price: {_fmt_money(dcf['implied_price'])}**",
            "",
        ]

    if result.get("relative"):
        rel = result["relative"]
        meds = rel.get("peer_medians", {})
        imp = rel.get("implied", {})
        lines += [
            "## Relative Valuation",
            f"- Peer medians ({meds.get('peer_count', 0)} peers): P/E {meds.get('pe')}, "
            f"EV/Rev {meds.get('ev_rev')}, EV/EBITDA {meds.get('ev_ebitda')}",
            f"- Implied: P/E {_fmt_money(imp.get('pe'))}, EV/Rev {_fmt_money(imp.get('ev_rev'))}, "
            f"EV/EBITDA {_fmt_money(imp.get('ev_ebitda'))}",
            f"- **Blended relative price: {_fmt_money(rel.get('implied_price'))}**",
            "",
        ]

    if result.get("rule_of_40") and result["rule_of_40"].get("score") is not None:
        r40 = result["rule_of_40"]
        lines += [
            "## Rule of 40",
            f"- Score: {r40['score']} ({r40['classification']})",
            "",
        ]

    if result.get("sotp"):
        sotp = result["sotp"]
        lines += ["## Sum-of-the-Parts"]
        for seg in sotp["segments"]:
            lines.append(f"- {seg['name']}: EV {_fmt_money(seg['ev'])} ({seg['basis']})")
        lines.append(f"- Equity value: {_fmt_money(sotp['equity_value'])}")
        lines.append(f"- **Implied SOTP price: {_fmt_money(sotp['implied_price'])}**")
        if sotp.get("conglomerate_discount") is not None:
            lines.append(f"- Conglomerate discount: {sotp['conglomerate_discount']:+.0%}")
        lines.append("")

    if result.get("sensitivity"):
        sens = result["sensitivity"]
        header = "| WACC \\ g | " + " | ".join(f"{g:.1%}" for g in sens["g_values"]) + " |"
        sep = "|---" * (len(sens["g_values"]) + 1) + "|"
        lines += ["## Sensitivity — implied price (WACC x terminal g)", header, sep]
        for row in sens["rows"]:
            cells = " | ".join(_fmt_money(p) if p is not None else "—" for p in row["prices"])
            lines.append(f"| {row['wacc']:.2%} | {cells} |")
        lines.append("")

    if result.get("scenarios"):
        sc = result["scenarios"]
        lines += [
            "## Scenarios",
            "| Scenario | Implied price | WACC | Terminal g |",
            "|---|---|---|---|",
        ]
        for name in ("bull", "base", "bear"):
            if name in sc:
                s = sc[name]
                lines.append(
                    f"| {name.title()} | {_fmt_money(s['implied_price'])} | "
                    f"{s['wacc']:.2%} | {s['terminal_g']:.2%} |"
                )
        lines.append("")

    if result.get("guardrail_warnings"):
        lines += ["## Guardrail Warnings"]
        for w in result["guardrail_warnings"]:
            lines.append(f"- {w}")
        lines.append("")

    return "\n".join(lines) + "\n"


# --- CLI ---------------------------------------------------------------------


def _load_json_file(path: str) -> object:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Triangulated intrinsic-value estimate (DCF + relative + optional SOTP)."
    )
    p.add_argument("ticker", help="US ticker symbol, e.g. AAPL")
    p.add_argument("--api-key", help="FMP API key (defaults to FMP_API_KEY env var)")
    p.add_argument(
        "--input-json",
        help="Path to FMP-shaped raw JSON (profile/income/balance/cashflow/ratios/peers). "
        "Bypasses the network — used for offline/reproducible runs and tests.",
    )
    p.add_argument("--yfinance", action="store_true", help="Use the keyless yfinance fallback")
    p.add_argument("--rf", type=float, help="Override the 10Y risk-free rate (decimal, e.g. 0.045)")
    p.add_argument("--erp", type=float, default=DEFAULT_ERP, help="Equity risk premium (decimal)")
    p.add_argument(
        "--terminal-growth",
        type=float,
        default=DEFAULT_TERMINAL_G,
        help="Terminal growth (decimal)",
    )
    p.add_argument(
        "--exit-multiple", type=float, default=DEFAULT_EXIT_MULTIPLE, help="Exit EV/EBITDA multiple"
    )
    p.add_argument("--y1-growth", type=float, help="Override Year-1 revenue growth (decimal)")
    p.add_argument("--segments-json", help="Path to a JSON list of SOTP segment dicts")
    p.add_argument(
        "--corporate-ev-deduction",
        type=float,
        default=0.0,
        help="Unallocated corporate EV to deduct in SOTP",
    )
    p.add_argument("--output-dir", default="reports/", help="Output directory (default: reports/)")
    return p


def _resolve_financials(args) -> tuple[CompanyFinancials, float | None]:
    """Return (financials, live_rf) based on the chosen data path."""
    live_rf = None
    if args.input_json:
        raw = _load_json_file(args.input_json)
        fin = normalize_fmp(raw, args.ticker)
        if isinstance(raw, dict) and raw.get("risk_free_rate") is not None:
            live_rf = float(raw["risk_free_rate"])
        return fin, live_rf
    if args.yfinance:
        return fetch_financials_yfinance(args.ticker), None
    api_key = args.api_key or os.getenv("FMP_API_KEY")
    if not api_key:
        print(
            "ERROR: no data source. Provide --input-json, --yfinance, or an FMP API key "
            "(FMP_API_KEY env var or --api-key).",
            file=sys.stderr,
        )
        sys.exit(1)
    fin = fetch_financials_fmp(args.ticker, api_key)
    live_rf = fetch_risk_free_rate_fmp(api_key)
    return fin, live_rf


def main() -> None:
    args = build_parser().parse_args()

    try:
        fin, live_rf = _resolve_financials(args)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    rf = args.rf if args.rf is not None else (live_rf if live_rf is not None else DEFAULT_RF)
    if args.rf is None and live_rf is None:
        print(f"WARN: using stale default risk-free rate {DEFAULT_RF:.2%}", file=sys.stderr)

    segments = None
    if args.segments_json:
        segments = _load_json_file(args.segments_json)

    try:
        result = value_company(
            fin,
            rf=rf,
            erp=args.erp,
            terminal_g=args.terminal_growth,
            exit_multiple=args.exit_multiple,
            y1_growth=args.y1_growth,
            segments=segments,
            corporate_ev_deduction=args.corporate_ev_deduction,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    stem = f"intrinsic_value_{fin.ticker}_{stamp}"
    json_path = os.path.join(args.output_dir, f"{stem}.json")
    md_path = os.path.join(args.output_dir, f"{stem}.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(generate_markdown_report(result))

    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    blended = result["blended"]
    if blended.get("fair_value") is not None:
        print(
            f"\n{fin.ticker}: blended fair value ${blended['fair_value']:,.2f} vs "
            f"${fin.current_price:,.2f} ({blended['upside_pct']:+.1%})"
        )
    for w in result["guardrail_warnings"]:
        print(f"WARN: {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
