"""Capital-rotation decision engine for the Portfolio Manager skill.

Decides when a strong fresh buy candidate should displace a weak, stale holding
in a capital-saturated book. The single most common way a good signal dies is
not "no signal" — it is "no room": the notional cap is hit or the max-position
count is reached, and capital was allocated first-come-first-served with no
ranking. This module ranks and rotates.

PURE DECISION FUNCTION — no network, no side effects, fully testable offline.
The intended deployment is SHADOW MODE first: the caller logs the returned
``RotationDecision`` (which is fully auditable) for a review period, and only
flips ``shadow_mode`` off once the decisions have been validated against live
outcomes.

Principle blend (adapted from classic trend-following discipline):
  - Ride winners: NEVER evict a position still working
    (``roe_pct >= protect_winner_roe_pct``) — let the trend run.
  - Cut what is not working: the eviction target is the WEAKEST non-winner
    (lowest ROE), and only after it has had ``min_hold_days`` to prove out
    (anti-churn).
  - Opportunity cost: only a genuinely strong fresh signal
    (``candidate_composite >= min_candidate_composite``) justifies paying the
    round-trip commission/slippage to rotate.
  - Never override a real risk veto: rotation fires ONLY when the candidate was
    blocked PURELY by capital constraints (notional / concurrency). If any other
    gate vetoed the entry (regime, liquidity, news, correlation, stop distance,
    conviction ...), the trade stays blocked.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

SCHEMA_VERSION = "1.0"

# Substrings that identify a capital-saturation block (case-insensitive).
# A candidate is rotatable ONLY if every blocked reason matches one of these;
# any non-matching reason is treated as a genuine risk veto and blocks rotation.
DEFAULT_CAPITAL_MARKERS: tuple[str, ...] = (
    "notional",  # notional / deployed-capital cap reached
    "buying power",  # insufficient buying power
    "insufficient cash",  # no free cash to deploy
    "capital cap",  # generic deployed-capital ceiling
    "max positions",  # concurrency cap: position count
    "max concurrent",  # concurrency cap
    "concurrency",  # concurrency cap
)


@dataclass(frozen=True)
class RotationDecision:
    """Auditable rotation verdict, suitable for shadow-mode logging.

    ``action`` is ``"evict_and_enter"`` when ``should_rotate`` is True, else
    ``"hold"``. ``shadow_mode`` records whether the caller intends to only log
    (True) or act (False) on this decision.
    """

    should_rotate: bool
    action: str
    enter_symbol: str
    candidate_composite: float
    evict_symbol: str | None
    evict_roe_pct: float | None
    evict_age_days: float | None
    reason: str
    shadow_mode: bool = True
    schema_version: str = SCHEMA_VERSION
    considered: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation for shadow-mode logging."""
        return asdict(self)


def _is_capital_only(blocked_reasons: list[str], capital_markers: tuple[str, ...]) -> bool:
    """True only if EVERY blocked reason is a capital-saturation marker."""
    lowered = [str(r).lower() for r in blocked_reasons]
    return all(any(m in r for m in capital_markers) for r in lowered)


def decide_rotation(
    *,
    candidate_symbol: str,
    candidate_composite: float,
    blocked_reasons: list[str],
    open_positions: list[dict[str, Any]],
    min_candidate_composite: float,
    min_hold_days: float,
    protect_winner_roe_pct: float,
    capital_markers: tuple[str, ...] = DEFAULT_CAPITAL_MARKERS,
    shadow_mode: bool = True,
) -> RotationDecision:
    """Decide whether to rotate the weakest non-winner out for ``candidate_symbol``.

    Args:
        candidate_symbol: Ticker of the fresh buy candidate.
        candidate_composite: The candidate's composite score (from a screener).
        blocked_reasons: Why the candidate could NOT be entered. Rotation fires
            only if EVERY reason is a capital-saturation marker.
        open_positions: Current holdings; each dict needs ``symbol`` (or
            ``ticker``), ``roe_pct`` (return on equity / unrealized P&L %), and
            ``age_days`` (calendar days held). Missing ROE/age default to 0.0,
            which conservatively makes a position ineligible for eviction
            (age 0 < any positive min_hold_days).
        min_candidate_composite: Minimum candidate score to justify a round trip.
        min_hold_days: A position must be held at least this long to be evictable.
        protect_winner_roe_pct: Positions with ROE at/above this are winners and
            are never evicted.
        capital_markers: Substrings that identify a capital-only block.
        shadow_mode: Recorded on the decision; True = log only, False = act.

    Returns:
        A ``RotationDecision``. ``should_rotate`` is False (action ``"hold"``)
        unless all guards pass, in which case the weakest eligible non-winner is
        named as the evictee.

    Raises:
        ValueError: on empty candidate symbol or negative thresholds.
    """
    if not candidate_symbol or not str(candidate_symbol).strip():
        raise ValueError("candidate_symbol must be a non-empty ticker")
    if min_hold_days < 0:
        raise ValueError("min_hold_days must be non-negative")

    candidate_symbol = str(candidate_symbol).strip().upper()

    def hold(reason: str) -> RotationDecision:
        return RotationDecision(
            should_rotate=False,
            action="hold",
            enter_symbol=candidate_symbol,
            candidate_composite=float(candidate_composite),
            evict_symbol=None,
            evict_roe_pct=None,
            evict_age_days=None,
            reason=reason,
            shadow_mode=shadow_mode,
            considered=[],
        )

    # 1. Only capital-saturation blocks are rotatable. A candidate that was not
    #    blocked at all needs no rotation; a candidate blocked by ANY real risk
    #    gate must stay blocked.
    if not blocked_reasons:
        return hold("candidate was not blocked — no rotation needed")
    if not _is_capital_only(blocked_reasons, capital_markers):
        return hold(
            "blocked by a non-capital gate (risk veto) — not rotatable: "
            + "; ".join(str(r) for r in blocked_reasons)
        )

    # 2. The fresh signal must be strong enough to justify the round-trip fees.
    if float(candidate_composite) < float(min_candidate_composite):
        return hold(
            f"candidate composite {float(candidate_composite):.1f} < "
            f"{float(min_candidate_composite):.1f} — not worth a rotation"
        )

    # 3. Find eligible evictees: not the candidate itself, not a winner
    #    (ride winners), and past the minimum hold (anti-churn).
    eligible: list[dict[str, Any]] = []
    considered: list[dict[str, Any]] = []
    for p in open_positions:
        sym = str(p.get("symbol") or p.get("ticker") or "").strip().upper()
        if not sym:
            continue
        roe = float(p.get("roe_pct", 0.0) or 0.0)
        age = float(p.get("age_days", 0.0) or 0.0)
        is_winner = roe >= protect_winner_roe_pct
        too_young = age < min_hold_days
        note = {
            "symbol": sym,
            "roe_pct": roe,
            "age_days": age,
            "eligible": not (sym == candidate_symbol or is_winner or too_young),
        }
        if sym == candidate_symbol:
            note["excluded_because"] = "same as candidate"
        elif is_winner:
            note["excluded_because"] = "protected winner"
        elif too_young:
            note["excluded_because"] = "below min hold"
        considered.append(note)
        if note["eligible"]:
            eligible.append({"symbol": sym, "roe_pct": roe, "age_days": age})

    if not eligible:
        return RotationDecision(
            should_rotate=False,
            action="hold",
            enter_symbol=candidate_symbol,
            candidate_composite=float(candidate_composite),
            evict_symbol=None,
            evict_roe_pct=None,
            evict_age_days=None,
            reason="no eligible evictee (all winners, too young to sell, or book empty)",
            shadow_mode=shadow_mode,
            considered=considered,
        )

    # 4. Evict the WEAKEST non-winner (lowest ROE). Deterministic tie-break:
    #    among equal ROE, prefer the STALER position (higher age), then symbol.
    weakest = min(eligible, key=lambda p: (p["roe_pct"], -p["age_days"], p["symbol"]))
    return RotationDecision(
        should_rotate=True,
        action="evict_and_enter",
        enter_symbol=candidate_symbol,
        candidate_composite=float(candidate_composite),
        evict_symbol=weakest["symbol"],
        evict_roe_pct=weakest["roe_pct"],
        evict_age_days=weakest["age_days"],
        reason=(
            f"rotate: evict {weakest['symbol']} "
            f"(roe {weakest['roe_pct']:+.1f}%, age {weakest['age_days']:.0f}d) "
            f"-> {candidate_symbol} (composite {float(candidate_composite):.1f})"
        ),
        shadow_mode=shadow_mode,
        considered=considered,
    )


def normalize_alpaca_positions(
    positions: list[dict[str, Any]],
    age_days_by_symbol: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Convert Alpaca ``/v2/positions`` records into rotation-ready dicts.

    Alpaca exposes unrealized P&L percent as ``unrealized_plpc`` (a decimal, so
    0.12 == +12%) but does NOT return hold age. Supply ``age_days_by_symbol`` to
    fill it (e.g. derived from filled-order timestamps); symbols missing from the
    map default to age 0.0, which conservatively protects them from eviction.
    """
    age_map = {k.upper(): v for k, v in (age_days_by_symbol or {}).items()}
    out: list[dict[str, Any]] = []
    for p in positions:
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym:
            continue
        out.append(
            {
                "symbol": sym,
                "roe_pct": float(p.get("unrealized_plpc", 0.0) or 0.0) * 100.0,
                "age_days": float(age_map.get(sym, 0.0)),
            }
        )
    return out


def generate_markdown_report(decision: RotationDecision) -> str:
    """Render a rotation decision as a markdown audit note."""
    d = decision
    lines = [
        "# Capital Rotation Decision",
        "**Generated:** {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "**Mode:** {}".format("SHADOW (log only)" if d.shadow_mode else "LIVE (act)"),
        "",
        "## Verdict",
        f"- **Action:** {d.action.upper()}",
        f"- **Candidate:** {d.enter_symbol} (composite {d.candidate_composite:.1f})",
    ]
    if d.should_rotate:
        lines.append(
            f"- **Evict:** {d.evict_symbol} (roe {d.evict_roe_pct or 0.0:+.1f}%, age {d.evict_age_days or 0.0:.0f}d)"
        )
    lines.append(f"- **Rationale:** {d.reason}")
    lines.append("")
    if d.considered:
        lines.append("## Positions Considered")
        lines.append("| Symbol | ROE % | Age (d) | Eligible | Excluded because |")
        lines.append("|--------|-------|---------|----------|------------------|")
        for c in d.considered:
            lines.append(
                "| {} | {:+.1f} | {:.0f} | {} | {} |".format(
                    c["symbol"],
                    c["roe_pct"],
                    c["age_days"],
                    "yes" if c["eligible"] else "no",
                    c.get("excluded_because", "-"),
                )
            )
        lines.append("")
    lines.append(
        "*Shadow-mode advisory. Not financial advice. Validate against live "
        "outcomes before disabling shadow mode.*"
    )
    return "\n".join(lines) + "\n"


def _load_json(path: str) -> Any:
    with open(path) as f:
        return json.load(f)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capital-rotation decision engine: decide whether a fresh buy "
            "candidate should evict the weakest stale holding when the book is "
            "capital-saturated. Shadow-mode advisory by default."
        )
    )
    parser.add_argument(
        "--candidate",
        required=True,
        help='JSON file with {"symbol","composite","blocked_reasons":[...]}',
    )
    parser.add_argument(
        "--positions",
        required=True,
        help=(
            "JSON file: a list of holdings. Either rotation shape "
            '({"symbol","roe_pct","age_days"}) or raw Alpaca positions '
            "(use --alpaca to normalize)."
        ),
    )
    parser.add_argument(
        "--alpaca",
        action="store_true",
        help="Treat --positions as raw Alpaca /v2/positions records and normalize.",
    )
    parser.add_argument(
        "--age-days-json",
        help="Optional JSON map {symbol: age_days} used with --alpaca.",
    )
    parser.add_argument(
        "--min-candidate-composite",
        type=float,
        default=70.0,
        help="Minimum candidate composite score to justify a rotation (default: 70).",
    )
    parser.add_argument(
        "--min-hold-days",
        type=float,
        default=5.0,
        help="A holding must be held at least this many days to be evictable (default: 5).",
    )
    parser.add_argument(
        "--protect-winner-roe-pct",
        type=float,
        default=15.0,
        help="Holdings with ROE at/above this are winners, never evicted (default: 15).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Mark the decision LIVE (act) instead of the default SHADOW (log only).",
    )
    parser.add_argument(
        "--output-dir",
        default="reports/",
        help="Output directory for the decision report (default: reports/).",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        candidate = _load_json(args.candidate)
        raw_positions = _load_json(args.positions)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading input JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if args.alpaca:
        age_map = None
        if args.age_days_json:
            try:
                age_map = _load_json(args.age_days_json)
            except (OSError, json.JSONDecodeError) as e:
                print(f"Error reading --age-days-json: {e}", file=sys.stderr)
                sys.exit(1)
        positions = normalize_alpaca_positions(raw_positions, age_map)
    else:
        positions = raw_positions

    try:
        decision = decide_rotation(
            candidate_symbol=candidate.get("symbol", ""),
            candidate_composite=float(candidate.get("composite", 0.0)),
            blocked_reasons=list(candidate.get("blocked_reasons", [])),
            open_positions=positions,
            min_candidate_composite=args.min_candidate_composite,
            min_hold_days=args.min_hold_days,
            protect_winner_roe_pct=args.protect_winner_roe_pct,
            shadow_mode=not args.live,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    json_path = os.path.join(args.output_dir, f"rotation_decision_{timestamp}.json")
    md_path = os.path.join(args.output_dir, f"rotation_decision_{timestamp}.md")
    with open(json_path, "w") as f:
        json.dump(decision.to_dict(), f, indent=2)
    with open(md_path, "w") as f:
        f.write(generate_markdown_report(decision))

    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    print(decision.reason)


if __name__ == "__main__":
    main()
