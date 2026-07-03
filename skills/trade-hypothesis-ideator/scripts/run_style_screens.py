#!/usr/bin/env python3
"""trade-hypothesis-ideator: multi-style idea-screen runner.

Turns each style recipe from ``references/style_factor_recipes.md`` into a
boolean filter spec compatible with the keyless yfinance screener
(``skills/finviz-screener/scripts/yf_boolean_screen.py``), optionally executes
it, and emits a per-candidate idea-card scaffold with the four required
sections (peer-relative metric table, mispricing bullets, catalyst,
disconfirming risks).

Design notes:
- The sibling screener is invoked via ``subprocess`` (never imported), so this
  runner does not depend on that module existing at import time.
- ``--execute`` is OFF by default: the runner builds the spec + the exact
  command and stays fully offline. Only ``--execute`` shells out (network).
- Recipe registry, spec building, command building, output parsing, idea-card
  and value-chain formatting are all pure and unit-tested against fixtures.

Field names are Yahoo ``EquityQuery`` fields (see
``skills/finviz-screener/references/yahoo_screener_fields.md``). Yields and
percent-change fields are fractions (``0.05`` = 5%).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

# Repo root: scripts/ -> trade-hypothesis-ideator/ -> skills/ -> <repo root>
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCREENER_SCRIPT = (
    REPO_ROOT / "skills" / "finviz-screener" / "scripts" / "yf_boolean_screen.py"
)

# ---------------------------------------------------------------------------
# Recipe registry
# ---------------------------------------------------------------------------
# Each recipe pairs a *mechanical* screen (fields the keyless screener can
# filter on) with the *qualitative* factors that need fundamental follow-up,
# plus the metadata used to build the required idea-card outputs.

STYLE_RECIPES: dict[str, dict[str, Any]] = {
    "value": {
        "style": "value",
        "direction": "long",
        "summary": "Cheap on cash-flow/asset terms with insiders buying.",
        "screen_spec": {
            "operator": "and",
            "operands": [
                {"operator": "lt", "operands": ["peratio.lasttwelvemonths", 18]},
                {"operator": "lt", "operands": ["pricebookratio.quarterly", 2.5]},
                {"operator": "gt", "operands": ["forward_dividend_yield", 0.02]},
                {"operator": "gt", "operands": ["intradaymarketcap", 1_000_000_000]},
            ],
        },
        "qualitative_factors": [
            "Free-cash-flow yield > 5% (FCF / market cap from cash-flow statement)",
            "Insider open-market buying in the last 90 days",
            "EV/EBITDA below the stock's own 5-year average",
        ],
        "peer_metrics": [
            "P/E",
            "EV/EBITDA",
            "P/B",
            "FCF yield",
            "Dividend yield",
            "Net debt/EBITDA",
        ],
        "mispricing_prompts": [
            "What is the market extrapolating that recent results contradict?",
            "Is the discount a cycle trough or a structural demand decline?",
            "Does insider buying corroborate that this is not a value trap?",
        ],
        "catalysts": [
            "Cost program / margin self-help",
            "Asset sale or spin-off",
            "Buyback authorization or dividend hike",
            "Cyclical trough inflecting",
        ],
        "disconfirming_risks": [
            "FCF yield is optical (one-off working-capital release)",
            "Insider buys are token-sized",
            "Cheapness reflects structural, not cyclical, decline",
        ],
    },
    "growth": {
        "style": "growth",
        "direction": "long",
        "summary": "Accelerating revenue with widening unit economics.",
        "screen_spec": {
            "operator": "and",
            "operands": [
                {"operator": "gt", "operands": ["totalrevenues1yrgrowth.lasttwelvemonths", 0.15]},
                {"operator": "gt", "operands": ["quarterlyrevenuegrowth.quarterly", 0.15]},
                {"operator": "gt", "operands": ["grossprofitmargin.lasttwelvemonths", 0.40]},
                {"operator": "gt", "operands": ["intradaymarketcap", 1_000_000_000]},
            ],
        },
        "qualitative_factors": [
            "Revenue acceleration: latest-Q YoY growth > prior-Q YoY (last 4-6 quarters)",
            "Margin expansion: gross/operating margin trending up over 4+ quarters",
            "ROIC > 15%; net revenue retention > 110% for subscription models",
        ],
        "peer_metrics": [
            "Revenue growth (LTM)",
            "Revenue growth (latest Q)",
            "Gross margin trend",
            "EV/sales (NTM)",
            "ROIC",
            "Net retention",
        ],
        "mispricing_prompts": [
            "Is the acceleration already in consensus estimates and the multiple?",
            "What durable driver (product, land, TAM) sustains the growth?",
            "Are margins expanding for structural reasons or mix/pull-forward?",
        ],
        "catalysts": [
            "Guidance raise",
            "New product ramp / TAM-expanding launch",
            "Large-customer land",
            "First quarter of GAAP profitability",
        ],
        "disconfirming_risks": [
            "Growth decelerates in the next print",
            "Margin gain was pull-forward or mix, not durable",
            "Net retention slips; multiple already discounts the acceleration",
        ],
    },
    "quality": {
        "style": "quality",
        "direction": "long",
        "summary": "Durable compounder bought on temporary dislocation.",
        "screen_spec": {
            "operator": "and",
            "operands": [
                {"operator": "gt", "operands": ["returnonequity.lasttwelvemonths", 0.15]},
                {"operator": "lt", "operands": ["totaldebtequity.lasttwelvemonths", 0.60]},
                {"operator": "gt", "operands": ["ebitdamargin.lasttwelvemonths", 0.20]},
                {"operator": "gt", "operands": ["intradaymarketcap", 2_000_000_000]},
            ],
        },
        "qualitative_factors": [
            "Consistent revenue growth over 5+ years (no single-year air pockets)",
            "Stable or expanding margins across a full cycle",
            "High FCF conversion (FCF / net income near or above 1.0)",
            "Insider ownership > 5%",
        ],
        "peer_metrics": [
            "ROE",
            "ROIC",
            "Gross/EBITDA margin",
            "Debt/EBITDA",
            "FCF conversion",
            "Insider ownership",
        ],
        "mispricing_prompts": [
            "Is the current headwind transient (FX, destocking, one bad quarter)?",
            "Is ROE margin-driven or leverage-driven?",
            "Is there reinvestment runway to extend the compounding?",
        ],
        "catalysts": [
            "Transient headwind resolving",
            "Reinvestment runway extension",
            "Capital-return step-up",
        ],
        "disconfirming_risks": [
            "ROE is leverage-driven, not margin-driven",
            "Moat erosion (share loss, pricing pressure)",
            "The 'temporary' headwind is structural",
        ],
    },
    "short": {
        "style": "short",
        "direction": "short",
        "summary": "Deteriorating fundamentals masked by an accrual/valuation gap.",
        "screen_spec": {
            "operator": "and",
            "operands": [
                {"operator": "gt", "operands": ["peratio.lasttwelvemonths", 40]},
                {"operator": "lt", "operands": ["totalrevenues1yrgrowth.lasttwelvemonths", 0.03]},
                {"operator": "gt", "operands": ["intradaymarketcap", 1_000_000_000]},
            ],
        },
        "qualitative_factors": [
            "Receivables/inventory growing faster than sales (rising DSO or days-inventory)",
            "Insider selling: accelerating open-market sales or 10b5-1 changes",
            "Valuation premium to peers with no growth/margin justification",
            "Accounting red flags (auditor change, restatement, widening non-GAAP)",
            "Crowding guard: check short_percentage_of_float.value and days_to_cover_short.value",
        ],
        "peer_metrics": [
            "P/E",
            "EV/sales",
            "Revenue growth",
            "DSO / days-inventory trend",
            "Gross-margin trend",
            "Short interest % float",
            "Days-to-cover",
        ],
        "mispricing_prompts": [
            "Is the accrual build channel stuffing or a legitimate contract ramp?",
            "What justifies the premium multiple against decelerating growth?",
            "Is the borrow available and short interest not already extreme?",
        ],
        "catalysts": [
            "Earnings miss / guide-down",
            "Receivables write-down",
            "Covenant breach",
            "Lockup expiry adding supply",
        ],
        "disconfirming_risks": [
            "Accrual build is a legitimate ramp (new contracts)",
            "A credible catalyst re-rates it higher",
            "Borrow is expensive or short interest is already extreme (squeeze risk)",
        ],
    },
    "special-situation": {
        "style": "special-situation",
        "direction": "either",
        "summary": "Event-driven dislocation; triggers are event-sourced, not screened.",
        "screen_spec": {
            "operator": "and",
            "operands": [
                {
                    "operator": "btwn",
                    "operands": ["intradaymarketcap", 300_000_000, 50_000_000_000],
                },
                {"operator": "gt", "operands": ["avgdailyvol3m", 200_000]},
            ],
        },
        "qualitative_factors": [
            "Spin-off completed in the last 12 months (forced selling, orphaned coverage)",
            "Recent IPO/SPAC with an upcoming lockup expiry",
            "Activist 13D/13D-A filed; proxy contest",
            "Emergence from restructuring/bankruptcy; fresh-start accounting",
            "Management change at a chronic underperformer",
        ],
        "peer_metrics": [
            "Sum-of-the-parts vs. EV",
            "Pro-forma leverage",
            "Stub valuation",
            "Comparable transaction multiples",
        ],
        "mispricing_prompts": [
            "Is the corporate event already fully reflected in the price?",
            "Does the cheap stub carry a hidden liability?",
            "Has forced selling run its course or does it have further to go?",
        ],
        "catalysts": [
            "The event date itself (spin completion, lockup date)",
            "Activist settlement",
            "Refinancing close",
            "First clean post-emergence quarter",
        ],
        "disconfirming_risks": [
            "The event is already fully reflected",
            "The 'cheap' stub carries a hidden liability",
            "Forced selling has further to run",
        ],
    },
}


# ---------------------------------------------------------------------------
# Pure: recipe access + spec building
# ---------------------------------------------------------------------------


def list_recipes() -> list[str]:
    """Return the sorted list of available recipe names."""
    return sorted(STYLE_RECIPES)


def get_recipe(name: str) -> dict[str, Any]:
    """Return the recipe definition for *name*.

    Raises:
        ValueError: If *name* is not a known recipe.
    """
    recipe = STYLE_RECIPES.get(name)
    if recipe is None:
        raise ValueError(f"Unknown recipe '{name}'. Available: {list_recipes()}")
    return recipe


def build_screen_spec(name: str, region: str | None = "us") -> dict[str, Any]:
    """Build the boolean filter spec for *name*, optionally AND-ing a region gate.

    The returned dict matches the shape consumed by ``yf_boolean_screen.py``
    (``--query-json``). *region* is a lowercase two-letter code (``us``); pass
    ``None`` (or empty) to leave the universe global.
    """
    recipe = get_recipe(name)
    spec = deepcopy(recipe["screen_spec"])
    if not region:
        return spec

    region_leaf = {"operator": "eq", "operands": ["region", region]}
    if spec.get("operator") == "and":
        return {"operator": "and", "operands": [*spec["operands"], region_leaf]}
    return {"operator": "and", "operands": [spec, region_leaf]}


# ---------------------------------------------------------------------------
# Pure: screener command + output parsing
# ---------------------------------------------------------------------------


def build_screener_command(
    spec: dict[str, Any],
    *,
    script_path: Path,
    count: int = 25,
    sort_field: str = "intradaymarketcap",
    sort_asc: bool = False,
    python_executable: str | None = None,
) -> list[str]:
    """Build the argv list that runs the sibling keyless screener for *spec*.

    Uses ``--no-report`` so the screener prints a summary JSON (with
    ``symbols``) to stdout rather than writing files of its own.
    """
    cmd = [
        python_executable or sys.executable,
        str(script_path),
        "--query-json",
        json.dumps(spec),
        "--count",
        str(count),
        "--sort-field",
        sort_field,
        "--no-report",
    ]
    if sort_asc:
        cmd.append("--sort-asc")
    return cmd


def parse_screener_output(stdout: str) -> dict[str, Any]:
    """Parse the summary JSON emitted by ``yf_boolean_screen.py --no-report``.

    Returns a dict with ``result_count``, ``total_matches`` and ``symbols``
    (symbols coerced to a clean list of non-empty strings).

    Raises:
        ValueError: If *stdout* is not valid JSON.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"screener output is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("screener output must be a JSON object")
    raw_symbols = payload.get("symbols") or []
    symbols = [str(s) for s in raw_symbols if isinstance(s, str) and s.strip()]
    return {
        "result_count": payload.get("result_count", len(symbols)),
        "total_matches": payload.get("total_matches"),
        "symbols": symbols,
    }


# ---------------------------------------------------------------------------
# Network (lazy path): shell out to the sibling screener
# ---------------------------------------------------------------------------


def run_screener(cmd: list[str], *, timeout: int = 120) -> dict[str, Any]:
    """Execute the screener *cmd* and return the parsed summary.

    Raises:
        FileNotFoundError: If the screener script is missing.
        RuntimeError: If the screener exits non-zero.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # noqa: S603
    if proc.returncode != 0:
        raise RuntimeError(
            f"screener exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return parse_screener_output(proc.stdout)


# ---------------------------------------------------------------------------
# Pure: idea-card scaffold + value-chain map
# ---------------------------------------------------------------------------


def build_idea_card(name: str, symbol: str) -> str:
    """Build a markdown idea-card scaffold for *symbol* under recipe *name*.

    Emits the four required sections (peer-relative metric table, mispricing
    bullets, catalyst, disconfirming risks) with cells/checklist prompts for
    the analyst to fill from fundamentals.
    """
    recipe = get_recipe(name)
    metrics = recipe["peer_metrics"]
    lines = [
        f"### {symbol} — {recipe['style']} — {recipe['direction']}",
        "",
        "_One-line thesis:_ _(fill in)_",
        "",
        "**Peer-relative metrics**",
        "",
        "| Metric | " + symbol + " | Peer A | Peer B | Sector median |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [f"| {m} |  |  |  |  |" for m in metrics]
    lines += ["", "**Mispricing bullets** _(3-5)_"]
    lines += [f"- {p}" for p in recipe["mispricing_prompts"]]
    lines += ["", "**Catalyst** _(pick the dated/conditional trigger)_"]
    lines += [f"- {c}" for c in recipe["catalysts"]]
    lines += ["", "**Disconfirming risks** _(become kill_criteria)_"]
    lines += [f"- {r}" for r in recipe["disconfirming_risks"]]
    lines += [
        "",
        "**Qualitative factors still to verify**",
    ]
    lines += [f"- [ ] {f}" for f in recipe["qualitative_factors"]]
    return "\n".join(lines) + "\n"


def build_value_chain_map(thesis: str, beneficiaries: list[dict[str, Any]]) -> str:
    """Render a thematic value-chain map from a prepared beneficiaries list.

    Each beneficiary dict may carry: ``ticker``, ``name``, ``layer``
    (``direct`` / ``indirect`` / ``second_order``), ``priced_in`` (bool),
    ``mechanism`` (transmission mechanism), ``note``. Names classified as
    *not priced-in* (especially second-order) are surfaced as the hunt list.
    """
    layer_order = ["direct", "indirect", "second_order"]
    layer_label = {
        "direct": "Direct",
        "indirect": "Indirect",
        "second_order": "Second-order",
    }
    lines = [f"# Value-Chain Sweep: {thesis}", "", "## Map", ""]
    lines.append("| Layer | Ticker | Name | Transmission mechanism | Priced-in? | Note |")
    lines.append("| --- | --- | --- | --- | --- | --- |")

    def _sort_key(b: dict[str, Any]) -> int:
        layer = str(b.get("layer", "")).lower()
        return layer_order.index(layer) if layer in layer_order else len(layer_order)

    for b in sorted(beneficiaries, key=_sort_key):
        layer = str(b.get("layer", "")).lower()
        priced = b.get("priced_in")
        priced_cell = "yes" if priced is True else "no" if priced is False else "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    layer_label.get(layer, layer or "-"),
                    str(b.get("ticker", "-")),
                    str(b.get("name", "-")),
                    str(b.get("mechanism", "-")),
                    priced_cell,
                    str(b.get("note", "-")),
                ]
            )
            + " |"
        )

    hunt = [
        b
        for b in beneficiaries
        if b.get("priced_in") is False and str(b.get("layer", "")).lower() != "direct"
    ]
    lines += ["", "## Hunt list — not-yet-connected (indirect / second-order)", ""]
    if hunt:
        for b in hunt:
            ticker = b.get("ticker", "-")
            mech = b.get("mechanism", "")
            suffix = f" — {mech}" if mech else ""
            lines.append(f"- **{ticker}** ({b.get('name', '')}){suffix}")
    else:
        lines.append("_No not-yet-connected names identified — the theme looks priced-in._")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Pure: report assembly
# ---------------------------------------------------------------------------


def build_recipe_report(results: list[dict[str, Any]], *, executed: bool, now: dt.datetime) -> str:
    """Assemble the multi-recipe markdown report from per-recipe *results*."""
    lines = ["# Style Factor Idea Screens", "", f"**Generated:** {now.isoformat()}", ""]
    lines.append(
        "_Mode: executed (candidates below)_"
        if executed
        else "_Mode: dry-run (specs + commands only; pass --execute to run)_"
    )
    lines.append("")
    for res in results:
        recipe = get_recipe(res["recipe"])
        lines += [
            f"## {recipe['style']} ({recipe['direction']})",
            "",
            recipe["summary"],
            "",
            "**Screen spec**",
            "",
            "```json",
            json.dumps(res["spec"], indent=2),
            "```",
            "",
        ]
        if not executed:
            lines += ["**Command**", "", "```bash", " ".join(res["command"]), "```", ""]
            continue
        symbols = res.get("symbols", [])
        lines.append(f"**Candidates ({len(symbols)}):** " + (", ".join(symbols) or "_none_"))
        lines.append("")
        for sym in symbols:
            lines.append(build_idea_card(res["recipe"], sym))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-style idea-screen runner for trade-hypothesis-ideator.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="List recipe names and exit.")
    group.add_argument("--recipe", help="Run a single recipe by name.")
    group.add_argument("--all", action="store_true", help="Run every recipe.")
    group.add_argument(
        "--value-chain-file",
        help="JSON file with {thesis, beneficiaries[]} to render a value-chain map.",
    )

    parser.add_argument("--region", default="us", help="Region gate (default: us; '' = global).")
    parser.add_argument(
        "--count", type=int, default=25, help="Max results per screen (default 25)."
    )
    parser.add_argument("--sort-field", default="intradaymarketcap", help="Screener sort field.")
    parser.add_argument("--sort-asc", action="store_true", help="Sort ascending.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run the sibling keyless screener (network). Off by default.",
    )
    parser.add_argument(
        "--screener-script",
        default=str(DEFAULT_SCREENER_SCRIPT),
        help="Path to yf_boolean_screen.py.",
    )
    parser.add_argument(
        "--output-dir", default="reports/", help="Output directory (default reports/)."
    )
    parser.add_argument(
        "--no-report", action="store_true", help="Skip writing report files; print summary only."
    )
    return parser.parse_args(argv)


def _write_report(output_dir: Path, prefix: str, now: dt.datetime, markdown: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    md_path = output_dir / f"{prefix}_{stamp}.md"
    md_path.write_text(markdown)
    return md_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)

    if args.list:
        print(json.dumps({"recipes": list_recipes()}, indent=2))
        return 0

    if args.value_chain_file:
        try:
            payload = json.loads(Path(args.value_chain_file).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error: cannot read value-chain file: {exc}", file=sys.stderr)
            return 1
        md = build_value_chain_map(
            str(payload.get("thesis", "(unstated thesis)")),
            payload.get("beneficiaries", []),
        )
        if not args.no_report:
            path = _write_report(Path(args.output_dir), "value_chain_map", now, md)
            print(json.dumps({"mode": "value-chain", "report_markdown": str(path)}, indent=2))
        else:
            print(md)
        return 0

    names = list_recipes() if args.all else [args.recipe]
    script_path = Path(args.screener_script)
    results: list[dict[str, Any]] = []

    for name in names:
        try:
            spec = build_screen_spec(name, region=args.region or None)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        command = build_screener_command(
            spec,
            script_path=script_path,
            count=args.count,
            sort_field=args.sort_field,
            sort_asc=args.sort_asc,
        )
        entry: dict[str, Any] = {"recipe": name, "spec": spec, "command": command}
        if args.execute:
            try:
                entry.update(run_screener(command))
            except (RuntimeError, ValueError, FileNotFoundError, subprocess.SubprocessError) as exc:
                print(f"Error: recipe '{name}' failed: {exc}", file=sys.stderr)
                return 1
        results.append(entry)

    md = build_recipe_report(results, executed=args.execute, now=now)
    summary: dict[str, Any] = {
        "mode": "execute" if args.execute else "dry-run",
        "recipes": names,
    }
    if args.execute:
        summary["candidates"] = {r["recipe"]: r.get("symbols", []) for r in results}
    if not args.no_report:
        path = _write_report(Path(args.output_dir), "style_screens", now, md)
        summary["report_markdown"] = str(path)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
