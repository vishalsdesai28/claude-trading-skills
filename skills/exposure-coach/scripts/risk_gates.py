#!/usr/bin/env python3
"""Composable risk gates for the Exposure Coach control plane.

Each gate is a *pure function* over a shared :class:`GateContext`, returning
``{"pass": bool, "reason"?: str}``. Gates are INDEPENDENT and ALL are evaluated
(no short-circuit) so every blocking reason is captured for telemetry, not just
the first one. :func:`eval_all_gates` collects per-gate results into a single
decision object.

The framework lets the Exposure Coach hand a single trade candidate through the
gates *after* the market-posture summary has set the ceiling but *before* the
order reaches execution. It consumes the documented JSON output of two sibling
skills:

  * ``liquidity-execution-cost`` -> average dollar volume for the liquidity floors
  * ``gdelt-news-catalyst``      -> the binary-news blackout flag

Pure calculation / parsing only: no network calls and no paid API. The module
imports with the standard library alone and its tests run fully offline against
committed fixtures.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GateResult = dict[str, Any]  # {"pass": bool, "reason"?: str, "note"?: str}


class GateContext:
    """Shared, read-only context every gate evaluates against.

    A candidate trade plus the portfolio / market state it would enter into.
    ``dollar_volume_usd`` is ``None`` when no liquidity-execution-cost data was
    supplied (the liquidity floors then treat the name as "unknown" and do not
    block on missing data).
    """

    def __init__(
        self,
        *,
        ticker: str,
        side: str,  # "long" or "short"
        confidence: float,  # 0.0 - 1.0
        trade_notional_usd: float,
        sector: str = "",
        current_positions: list[dict[str, Any]] | None = None,
        equity: float = 0.0,
        daily_pnl: float = 0.0,
        peak_daily_pnl: float = 0.0,
        dollar_volume_usd: float | None = None,
        trend_aligned: bool = False,
        has_news_blackout: bool = False,
        news_reason: str = "",
        last_trade_epoch_ms: int | None = None,
    ):
        self.ticker = (ticker or "").upper()
        self.side = (side or "long").lower()
        self.confidence = float(confidence or 0.0)
        self.trade_notional_usd = float(trade_notional_usd or 0.0)
        self.sector = sector or ""
        self.current_positions = current_positions or []
        self.equity = float(equity or 0.0)
        self.daily_pnl = float(daily_pnl or 0.0)
        self.peak_daily_pnl = float(peak_daily_pnl or 0.0)
        self.dollar_volume_usd = None if dollar_volume_usd is None else float(dollar_volume_usd)
        # True iff the candidate's direction agrees with the market posture
        # (long into NEW_ENTRY_ALLOWED, short into REDUCE_ONLY/CASH_PRIORITY).
        # Trend-aligned trades earn a LOWER confidence bar — the whole point of
        # a regime-aware floor is to demand full conviction only to fight the
        # tape, never to sit out a move that runs with it.
        self.trend_aligned = bool(trend_aligned)
        self.has_news_blackout = bool(has_news_blackout)
        self.news_reason = news_reason or ""
        self.last_trade_epoch_ms = last_trade_epoch_ms


# --------------------------------------------------------------------------- #
# Individual gates — each pure, each returns {"pass": bool, "reason"?: str}    #
# --------------------------------------------------------------------------- #


def confidence_gate(ctx: GateContext, min_confidence: float) -> GateResult:
    """Block low-conviction candidates. The effective floor is chosen by the
    caller (see :func:`eval_all_gates`) and is lower for trend-aligned trades."""
    if ctx.confidence >= min_confidence:
        return {"pass": True}
    return {
        "pass": False,
        "reason": f"confidence {ctx.confidence:.2f} < floor {min_confidence:.2f}"
        + (" (trend-aligned bar)" if ctx.trend_aligned else ""),
    }


def max_concurrent_positions_gate(ctx: GateContext, max_concurrent: int) -> GateResult:
    """Cap the number of simultaneous open positions."""
    if max_concurrent <= 0:
        return {"pass": True}
    held = len(ctx.current_positions)
    if held < max_concurrent:
        return {"pass": True}
    return {
        "pass": False,
        "reason": f"max concurrent positions reached ({held}/{max_concurrent})",
    }


def per_trade_notional_cap_gate(ctx: GateContext, cap_usd: float) -> GateResult:
    """Cap the dollar notional of a single trade. ``cap_usd <= 0`` disables."""
    cap = float(cap_usd or 0.0)
    if cap <= 0:
        return {"pass": True}
    # Position sizing rounds to whole/again-fractional shares, so an intended
    # $8,000 notional can land at $8,003. Allow a small tolerance; larger
    # overshoots still block.
    precision_tolerance = max(1.0, cap * 0.005)
    if ctx.trade_notional_usd <= cap + precision_tolerance:
        return {"pass": True}
    return {
        "pass": False,
        "reason": f"trade notional ${ctx.trade_notional_usd:,.0f} exceeds cap ${cap:,.0f}",
    }


def daily_loss_kill_switch(ctx: GateContext, max_daily_loss: float) -> GateResult:
    """Halt new entries once the day's realized+open P&L breaches a loss limit.

    ``max_daily_loss`` is a NEGATIVE dollar threshold (e.g. -3000). A
    non-negative value disables the switch so a flat/green day never blocks.
    """
    if max_daily_loss >= 0:
        return {"pass": True}
    if ctx.daily_pnl > max_daily_loss:
        return {"pass": True}
    return {
        "pass": False,
        "reason": (
            f"daily-loss kill switch: P&L ${ctx.daily_pnl:,.0f} <= limit ${max_daily_loss:,.0f}"
        ),
    }


def daily_giveback_gate(ctx: GateContext, halt_pct: float, min_peak_usd: float) -> GateResult:
    """Lock in a green day. Once the day's P&L has peaked at >= ``min_peak_usd``,
    block NEW entries if it then retraces more than ``halt_pct`` from that peak.
    Existing positions keep riding their own stops; this only stops opening
    fresh risk so a won day cannot fully round-trip. ``halt_pct <= 0`` disables.
    """
    if halt_pct <= 0 or ctx.peak_daily_pnl < min_peak_usd:
        return {"pass": True}
    floor = ctx.peak_daily_pnl * (1.0 - halt_pct)
    if ctx.daily_pnl <= floor:
        return {
            "pass": False,
            "reason": (
                f"daily give-back halt: P&L ${ctx.daily_pnl:,.0f} retraced "
                f">{halt_pct * 100:.0f}% from peak ${ctx.peak_daily_pnl:,.0f} "
                f"(floor ${floor:,.0f}) — no new entries today"
            ),
        }
    return {"pass": True}


def liquidity_floor(ctx: GateContext, min_dollar_volume: float) -> GateResult:
    """Block names whose average daily dollar volume is below a floor.

    Reads the dollar volume that ``liquidity-execution-cost`` reports. When no
    liquidity data was supplied (``dollar_volume_usd is None``) the floor is NOT
    enforced — a missing upstream signal must not silently block every trade.
    ``min_dollar_volume <= 0`` disables the gate entirely.
    """
    if min_dollar_volume <= 0:
        return {"pass": True}
    if ctx.dollar_volume_usd is None:
        return {"pass": True, "note": "liquidity data unavailable — floor not enforced"}
    if ctx.dollar_volume_usd >= min_dollar_volume:
        return {"pass": True}
    return {
        "pass": False,
        "reason": (
            f"avg $vol ${ctx.dollar_volume_usd / 1e6:.1f}M below floor "
            f"${min_dollar_volume / 1e6:.1f}M"
        ),
    }


def short_liquidity_floor(ctx: GateContext, min_short_dollar_volume: float) -> GateResult:
    """SEPARATE, deeper liquidity floor for SHORTS — thin names squeeze.

    A short in a thin name can be squeezed to its stop far more easily than a
    long can survive a thin pump, so shorts must clear a materially higher
    dollar-volume floor than longs. Applies ONLY to shorts; ``0`` disables
    (opt-in, reversible).
    """
    if ctx.side != "short" or min_short_dollar_volume <= 0:
        return {"pass": True}
    if ctx.dollar_volume_usd is None:
        return {"pass": True, "note": "liquidity data unavailable — short floor not enforced"}
    if ctx.dollar_volume_usd >= min_short_dollar_volume:
        return {"pass": True}
    return {
        "pass": False,
        "reason": (
            f"short on thin name: avg $vol ${ctx.dollar_volume_usd / 1e6:.1f}M "
            f"< short floor ${min_short_dollar_volume / 1e6:.1f}M (squeeze risk)"
        ),
    }


def correlation_cap(ctx: GateContext, max_correlated: int) -> GateResult:
    """Cap same-direction, same-sector exposure. Positions in the same sector
    on the same side are treated as correlated; too many concentrates risk in
    one macro theme. Unknown candidate sector or ``max_correlated <= 0`` skips.
    """
    if max_correlated <= 0 or not ctx.sector:
        return {"pass": True}
    sector = ctx.sector.lower()
    same = sum(
        1
        for p in ctx.current_positions
        if str(p.get("sector") or "").lower() == sector
        and str(p.get("side") or "").lower() == ctx.side
    )
    if same < max_correlated:
        return {"pass": True}
    return {
        "pass": False,
        "reason": (
            f"sector correlation cap reached ({same}/{max_correlated} {ctx.side} in {ctx.sector})"
        ),
    }


def cooldown_gate(
    ctx: GateContext, cooldown_min: float, now_epoch_ms: int | None = None
) -> GateResult:
    """Enforce a minimum wait between trades. ``now_epoch_ms`` is injectable so
    tests stay deterministic; it defaults to wall-clock time."""
    if cooldown_min <= 0 or ctx.last_trade_epoch_ms is None:
        return {"pass": True}
    now = now_epoch_ms if now_epoch_ms is not None else int(time.time() * 1000)
    elapsed_min = (now - ctx.last_trade_epoch_ms) / 60_000
    if elapsed_min >= cooldown_min:
        return {"pass": True}
    remaining = int(cooldown_min - elapsed_min)
    return {"pass": False, "reason": f"cooldown active ({remaining}min remaining)"}


def opposite_direction_guard(ctx: GateContext, allow_pyramid: bool = False) -> GateResult:
    """Block re-entry on a ticker already held. An open position is managed by
    its own plan; the coach never auto-flips it (opposite side) nor, by default,
    pyramids into it (same side). Set ``allow_pyramid`` to permit same-side adds
    while still blocking a flip.
    """
    existing = next(
        (p for p in ctx.current_positions if str(p.get("ticker") or "").upper() == ctx.ticker),
        None,
    )
    if not existing:
        return {"pass": True}
    existing_side = str(existing.get("side") or "").lower()
    if existing_side != ctx.side:
        return {
            "pass": False,
            "reason": f"opposite position exists ({ctx.ticker} {existing_side}) — no auto-flip",
        }
    if allow_pyramid:
        return {"pass": True}
    return {
        "pass": False,
        "reason": f"already holding {ctx.ticker} {existing_side} — no pyramid/re-entry",
    }


def news_blackout_gate(ctx: GateContext) -> GateResult:
    """Stand down when gdelt-news-catalyst flags a binary news event (earnings,
    Fed, M&A, halt, litigation) on the candidate."""
    if not ctx.has_news_blackout:
        return {"pass": True}
    detail = f" — {ctx.news_reason}" if ctx.news_reason else ""
    return {
        "pass": False,
        "reason": f"binary news blackout{detail} — standing down",
    }


# --------------------------------------------------------------------------- #
# Config + orchestration                                                      #
# --------------------------------------------------------------------------- #


def _cfg(config: dict[str, Any], key: str, default: Any) -> Any:
    """Read a config value tolerating snake_case OR camelCase keys."""
    if key in config:
        return config[key]
    parts = key.split("_")
    camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
    return config[camel] if camel in config else default


def eval_all_gates(
    ctx: GateContext,
    config: dict[str, Any] | None = None,
    now_epoch_ms: int | None = None,
) -> dict[str, Any]:
    """Evaluate every gate (no short-circuit) and return a decision object.

    Returns a dict with per-gate ``results``, ``passed_gates``, ``failed_gates``,
    aggregated ``block_reasons``, and ``approved`` / ``blocked`` booleans.
    """
    config = config or {}
    results: dict[str, GateResult] = {}

    # Regime-aware confidence floor: a trend-aligned candidate earns the lower
    # `aligned_min_confidence` bar; anything fighting the tape keeps the full
    # `min_confidence`.
    min_conf = float(_cfg(config, "min_confidence", 0.65))
    aligned = _cfg(config, "aligned_min_confidence", None)
    if ctx.trend_aligned and aligned is not None:
        min_conf = min(min_conf, float(aligned))
    results["confidence"] = confidence_gate(ctx, min_conf)

    results["max_concurrent"] = max_concurrent_positions_gate(
        ctx, int(_cfg(config, "max_concurrent", 5))
    )

    # Per-trade notional cap: absolute USD wins; else a % of equity.
    cap = _cfg(config, "max_trade_notional_usd", None)
    if cap is None:
        cap = float(_cfg(config, "max_trade_notional_pct", 0.1)) * ctx.equity
    results["notional_cap"] = per_trade_notional_cap_gate(ctx, float(cap or 0.0))

    # Daily-loss limit: absolute USD wins; else a % of equity (negative floor).
    max_daily_loss = _cfg(config, "max_daily_loss_usd", None)
    if max_daily_loss is None:
        max_daily_loss = -abs(float(_cfg(config, "max_daily_loss_pct", 0.03))) * ctx.equity
    results["daily_loss"] = daily_loss_kill_switch(ctx, float(max_daily_loss))

    results["daily_giveback"] = daily_giveback_gate(
        ctx,
        float(_cfg(config, "daily_giveback_halt_pct", 0.0) or 0.0),
        float(_cfg(config, "daily_giveback_min_peak_usd", 0.0) or 0.0),
    )

    results["liquidity"] = liquidity_floor(
        ctx, float(_cfg(config, "min_dollar_volume_usd", 0.0) or 0.0)
    )
    results["short_liquidity"] = short_liquidity_floor(
        ctx, float(_cfg(config, "min_short_dollar_volume_usd", 0.0) or 0.0)
    )

    results["correlation"] = correlation_cap(ctx, int(_cfg(config, "max_sector_correlated", 3)))
    results["cooldown"] = cooldown_gate(
        ctx, float(_cfg(config, "cooldown_min", 0.0) or 0.0), now_epoch_ms
    )
    results["opposite_guard"] = opposite_direction_guard(
        ctx, bool(_cfg(config, "allow_pyramid", False))
    )
    results["news"] = news_blackout_gate(ctx)

    passed: list[str] = []
    failed: list[str] = []
    block_reasons: list[str] = []
    for key, result in results.items():
        if result.get("pass"):
            passed.append(key)
        else:
            failed.append(key)
            block_reasons.append(result.get("reason", key))

    blocked = len(failed) > 0
    return {
        "ticker": ctx.ticker,
        "side": ctx.side,
        "trend_aligned": ctx.trend_aligned,
        "approved": not blocked,
        "blocked": blocked,
        "results": results,
        "passed_gates": passed,
        "failed_gates": failed,
        "block_reasons": block_reasons,
    }


# --------------------------------------------------------------------------- #
# Building a context from upstream skill JSON                                 #
# --------------------------------------------------------------------------- #


def is_trend_aligned(recommendation: str | None, side: str) -> bool:
    """Decide whether a candidate agrees with the exposure-coach posture.

    Longs are aligned when the posture recommendation is ``NEW_ENTRY_ALLOWED``;
    shorts are aligned when it is ``REDUCE_ONLY`` or ``CASH_PRIORITY``.
    """
    rec = (recommendation or "").upper()
    side = (side or "").lower()
    if side == "long":
        return rec == "NEW_ENTRY_ALLOWED"
    if side == "short":
        return rec in ("CASH_PRIORITY", "REDUCE_ONLY")
    return False


def extract_dollar_volume(liquidity: dict[str, Any] | None) -> float | None:
    """Pull average dollar volume from a liquidity-execution-cost JSON, tolerant
    of several field names and one level of nesting under ``liquidity``."""
    if not liquidity:
        return None
    for key in (
        "dollar_volume_usd",
        "avg_dollar_volume_usd",
        "adv_usd",
        "dollar_volume",
        "advDollarUsd",
    ):
        value = liquidity.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    inner = liquidity.get("liquidity")
    if isinstance(inner, dict):
        return extract_dollar_volume(inner)
    return None


def extract_news_blackout(news: dict[str, Any] | None) -> tuple[bool, str]:
    """Read a gdelt-news-catalyst JSON into (blackout, reason).

    Honors an explicit boolean flag under several names; otherwise infers a
    blackout from a high/critical catalyst severity.
    """
    if not news:
        return False, ""
    flag: bool | None = None
    for key in (
        "blackout",
        "has_blackout",
        "binary_news_risk",
        "hasBinaryNewsRisk",
        "blackout_active",
    ):
        if key in news:
            flag = bool(news[key])
            break
    if flag is None:
        severity = str(news.get("severity") or news.get("catalyst_severity") or "").lower()
        flag = severity in ("high", "critical")
    reason = str(
        news.get("headline")
        or news.get("catalyst")
        or news.get("catalyst_type")
        or news.get("reason")
        or ""
    )
    return bool(flag), reason


def build_context(
    candidate: dict[str, Any],
    portfolio: dict[str, Any] | None = None,
    liquidity: dict[str, Any] | None = None,
    news: dict[str, Any] | None = None,
    posture: dict[str, Any] | None = None,
) -> GateContext:
    """Assemble a :class:`GateContext` from the documented JSON shapes of the
    candidate, portfolio state, and the three upstream skills."""
    portfolio = portfolio or {}
    positions = portfolio.get("positions") or portfolio.get("current_positions") or []
    side = str(candidate.get("side", "long")).lower()
    recommendation = (posture or {}).get("recommendation")
    blackout, news_reason = extract_news_blackout(news)
    notional = candidate.get("trade_notional_usd", candidate.get("notional_usd", 0))
    return GateContext(
        ticker=str(candidate.get("ticker", "")),
        side=side,
        confidence=float(candidate.get("confidence", 0.0) or 0.0),
        trade_notional_usd=float(notional or 0.0),
        sector=str(candidate.get("sector", "") or ""),
        current_positions=positions,
        equity=float(portfolio.get("equity", 0.0) or 0.0),
        daily_pnl=float(portfolio.get("daily_pnl", 0.0) or 0.0),
        peak_daily_pnl=float(portfolio.get("peak_daily_pnl", 0.0) or 0.0),
        dollar_volume_usd=extract_dollar_volume(liquidity),
        trend_aligned=is_trend_aligned(recommendation, side),
        has_news_blackout=blackout,
        news_reason=news_reason,
        last_trade_epoch_ms=portfolio.get("last_trade_epoch_ms"),
    )


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def _load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists():
        print(f"Warning: file not found: {path}", file=sys.stderr)
        return None
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: could not load {path}: {exc}", file=sys.stderr)
        return None


def generate_gate_markdown(decision: dict[str, Any], generated_at: str) -> str:
    """One-page risk-gate decision report."""
    verdict = "APPROVED" if decision["approved"] else "BLOCKED"
    lines = [
        f"# Risk Gate Decision — {decision['ticker']} {decision['side'].upper()}",
        f"**Generated:** {generated_at[:19]}Z | **Decision:** {verdict} | "
        f"**Trend-aligned:** {'yes' if decision['trend_aligned'] else 'no'}",
        "",
        "| Gate | Result | Detail |",
        "|------|--------|--------|",
    ]
    for gate, result in decision["results"].items():
        mark = "PASS" if result.get("pass") else "BLOCK"
        detail = result.get("reason") or result.get("note") or ""
        lines.append(f"| {gate} | {mark} | {detail} |")
    lines.append("")
    if decision["blocked"]:
        lines.append("## Blocked — reasons")
        lines.extend(f"- {reason}" for reason in decision["block_reasons"])
    else:
        lines.append("## Approved — all gates passed")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a trade candidate through the Exposure Coach risk gates."
    )
    parser.add_argument("--candidate", type=Path, help="Candidate trade JSON")
    parser.add_argument("--ticker", help="Candidate ticker (if no --candidate file)")
    parser.add_argument("--side", default="long", choices=["long", "short"])
    parser.add_argument("--confidence", type=float, help="Candidate confidence 0-1")
    parser.add_argument("--notional", type=float, help="Trade notional USD")
    parser.add_argument("--sector", default="", help="Candidate sector")
    parser.add_argument("--posture", type=Path, help="exposure-coach posture JSON")
    parser.add_argument("--portfolio", type=Path, help="Portfolio / account-state JSON")
    parser.add_argument("--liquidity", type=Path, help="liquidity-execution-cost JSON")
    parser.add_argument("--news", type=Path, help="gdelt-news-catalyst JSON")
    parser.add_argument("--config", type=Path, help="Gate config JSON")
    parser.add_argument("--now-ms", type=int, help="Injected wall-clock epoch ms (cooldown)")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--json-only", action="store_true", help="Skip markdown output")
    args = parser.parse_args()

    candidate = _load_json(args.candidate) or {}
    if args.ticker:
        candidate = {
            "ticker": args.ticker,
            "side": args.side,
            "confidence": args.confidence if args.confidence is not None else 0.0,
            "trade_notional_usd": args.notional or 0.0,
            "sector": args.sector,
        }
    if not candidate.get("ticker"):
        print("Error: provide --candidate JSON or --ticker.", file=sys.stderr)
        return 1

    portfolio = _load_json(args.portfolio)
    liquidity = _load_json(args.liquidity)
    news = _load_json(args.news)
    posture = _load_json(args.posture)
    config = _load_json(args.config) or {}

    ctx = build_context(candidate, portfolio, liquidity, news, posture)
    decision = eval_all_gates(ctx, config, now_epoch_ms=args.now_ms)

    now = datetime.now(timezone.utc)
    decision["generated_at"] = now.isoformat()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = now.strftime("%Y-%m-%d_%H%M%S")
    stub = f"risk_gate_decision_{ctx.ticker}_{timestamp}"

    json_path = args.output_dir / f"{stub}.json"
    with open(json_path, "w") as handle:
        json.dump(decision, handle, indent=2)
    print(f"JSON report: {json_path}")

    if not args.json_only:
        md_path = args.output_dir / f"{stub}.md"
        with open(md_path, "w") as handle:
            handle.write(generate_gate_markdown(decision, decision["generated_at"]))
        print(f"Markdown report: {md_path}")

    verdict = "APPROVED" if decision["approved"] else "BLOCKED"
    print(f"\n{ctx.ticker} {ctx.side.upper()}: {verdict}")
    if decision["blocked"]:
        for reason in decision["block_reasons"]:
            print(f"  - {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
