"""Deterministic connective tissue for the adversarial-trade-debate skill.

The debate itself is LLM-orchestrated (see SKILL.md and references/). This
module carries the two *deterministic* jobs around that debate so they are
reproducible and testable offline:

1. ``assemble`` — read the DOCUMENTED JSON output shapes of the upstream
   analyst skills (intrinsic-value-dcf, retail-sentiment-ingestor) plus a
   technical-analyst report and optional news / prior-lessons text, and fold
   them into a single "debate brief" (JSON + markdown) that Stage 1
   (bull-vs-bear) reads. No sibling module is imported; only their on-disk
   output is consumed.

2. ``parse-decision`` — recover the structured decision fields from the
   *deterministic headers* the judges emit (see references/schemas.md). This
   is the free-text fallback: when a provider cannot return typed JSON, the
   Research Manager / Portfolio Manager still print ``**Rating**: ...`` style
   headers, and this parser turns those back into a machine record. It then
   builds the hand-off hint for the position-sizer skill.

Pure standard library, no network. Every function is import-safe and unit
tested against committed fixtures.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

# 5-tier forced rating shared by the Research Manager and Portfolio Manager.
RATING_VALUES = ["Buy", "Overweight", "Hold", "Underweight", "Sell"]
# 3-tier transaction direction used inside a concrete proposal.
ACTION_VALUES = ["Buy", "Hold", "Sell"]
# Ratings/actions that warrant a long-side position-sizer hand-off.
BULLISH_RATINGS = {"Buy", "Overweight"}
BULLISH_ACTIONS = {"Buy"}

# LLMs sometimes emit a placeholder string instead of omitting a numeric field.
_NULLISH = {"", "none", "n/a", "na", "null", "nil", "-", "tbd", "unknown", "—"}


# --------------------------------------------------------------------------- #
# Small coercion helpers
# --------------------------------------------------------------------------- #


def coerce_float(value):
    """Best-effort parse of a price-like value to float, else None.

    Tolerates currency symbols, thousands separators, a trailing percent sign,
    and the nullish placeholders models sometimes write into optional fields.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[*_`]", "", str(value)).strip()
    cleaned = cleaned.replace("$", "").replace(",", "").replace("%", "").strip()
    if cleaned.lower() in _NULLISH:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def match_enum(value, allowed):
    """Return the canonical enum member matching ``value`` (case-insensitive), else None.

    Accepts an exact match or a value that *starts with* the member followed by
    a separator, so "Buy — strong conviction" resolves to "Buy".
    """
    if value is None:
        return None
    cleaned = re.sub(r"[*_`]", "", str(value)).strip().lower()
    for member in allowed:
        low = member.lower()
        if cleaned == low or re.match(rf"{re.escape(low)}\b", cleaned):
            return member
    return None


def load_json(path):
    """Load a UTF-8 JSON file."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_text(path):
    """Load a UTF-8 text file, stripped of trailing whitespace."""
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


# --------------------------------------------------------------------------- #
# Stage-1 input summarizers (consume documented sibling output shapes)
# --------------------------------------------------------------------------- #


def summarize_dcf(dcf: dict) -> dict:
    """Compact the intrinsic-value-dcf JSON into the valuation lane of the brief.

    Consumes the documented keys: ``snapshot``, ``sector_routing``,
    ``blended`` (fair_value / upside_pct), ``scenarios``, ``guardrail_warnings``.
    """
    snap = dcf.get("snapshot") or {}
    blended = dcf.get("blended") or {}
    return {
        "current_price": snap.get("current_price"),
        "sector": snap.get("sector"),
        "fair_value": blended.get("fair_value"),
        "upside_pct": blended.get("upside_pct"),
        "sector_routing": dcf.get("sector_routing"),
        "scenarios": dcf.get("scenarios"),
        "guardrail_warnings": dcf.get("guardrail_warnings") or [],
    }


def summarize_sentiment(report: dict, ticker: str) -> dict | None:
    """Pull one ticker's row from a retail-sentiment-ingestor run report.

    Consumes the documented ``results[]`` shape (overall_band, overall_score,
    direction, confidence, divergence, contrarian_overextension). Returns None
    when the ticker is absent.
    """
    want = str(ticker).upper()
    for row in report.get("results") or []:
        if str(row.get("ticker", "")).upper() == want:
            return {
                "band": row.get("overall_band"),
                "score": row.get("overall_score"),
                "direction": row.get("direction"),
                "confidence": row.get("confidence"),
                "divergence": bool(row.get("divergence")),
                "contrarian_overextension": bool(row.get("contrarian_overextension")),
            }
    return None


def summarize_technical(content: str, is_json: bool = False) -> dict:
    """Wrap a technical-analyst report for the technicals lane.

    The technical-analyst skill emits a markdown report (no fixed JSON), so the
    default path embeds the prose verbatim. A caller that has a JSON summary can
    pass ``is_json=True`` to carry the parsed object instead.
    """
    if is_json:
        return {"format": "json", "data": json.loads(content)}
    return {"format": "markdown", "text": content.strip()}


def build_debate_brief(
    ticker: str,
    *,
    valuation: dict | None = None,
    technical: dict | None = None,
    sentiment: dict | None = None,
    news: str | None = None,
    proposal: dict | None = None,
    prior_lessons: str | None = None,
) -> dict:
    """Fold the analyst lanes into the Stage-1 debate brief.

    ``proposal`` (action/entry/stop/size) and ``prior_lessons`` are optional and
    only matter for Stage 2 (the risk debate); they ride along here so a single
    brief documents the whole run.
    """
    lanes_present = [
        name
        for name, val in (
            ("valuation", valuation),
            ("technical", technical),
            ("sentiment", sentiment),
            ("news", news),
        )
        if val
    ]
    return {
        "schema_version": "1.0",
        "source_skill": "adversarial_trade_debate",
        "stage": "analyst_inputs",
        "ticker": str(ticker).upper(),
        "lanes_present": lanes_present,
        "lanes_missing": [
            name
            for name in ("valuation", "technical", "sentiment", "news")
            if name not in lanes_present
        ],
        "valuation": valuation,
        "technical": technical,
        "sentiment": sentiment,
        "news": news,
        "proposal": proposal,
        "prior_lessons": prior_lessons,
    }


def _fmt(value, suffix=""):
    return f"{value}{suffix}" if value is not None else "n/a"


def render_brief_markdown(brief: dict) -> str:
    """Render the debate brief as the markdown the bull/bear debate reads."""
    lines = [
        f"# Adversarial Trade Debate — Brief: {brief['ticker']}",
        "",
        f"**Lanes present:** {', '.join(brief['lanes_present']) or 'none'}",
    ]
    if brief["lanes_missing"]:
        lines.append(
            f"**Lanes MISSING (debate must not fabricate these):** "
            f"{', '.join(brief['lanes_missing'])}"
        )
    lines.append("")

    val = brief.get("valuation")
    lines.append("## Valuation (intrinsic-value-dcf)")
    if val:
        upside = val.get("upside_pct")
        upside_s = f"{upside * 100:.1f}%" if isinstance(upside, (int, float)) else "n/a"
        lines += [
            f"- Current price: {_fmt(val.get('current_price'))}",
            f"- Blended fair value: {_fmt(val.get('fair_value'))} (upside {upside_s})",
            f"- Sector: {_fmt(val.get('sector'))}",
        ]
        for warn in val.get("guardrail_warnings") or []:
            lines.append(f"- Guardrail: {warn}")
    else:
        lines.append("- (not supplied)")
    lines.append("")

    lines.append("## Technicals (technical-analyst)")
    tech = brief.get("technical")
    if tech and tech.get("format") == "markdown":
        lines += [tech["text"]]
    elif tech and tech.get("format") == "json":
        lines += ["```json", json.dumps(tech["data"], indent=2), "```"]
    else:
        lines.append("- (not supplied)")
    lines.append("")

    lines.append("## Retail sentiment (retail-sentiment-ingestor)")
    sent = brief.get("sentiment")
    if sent:
        lines += [
            f"- Band: {_fmt(sent.get('band'))} (score {_fmt(sent.get('score'))}/10, "
            f"confidence {_fmt(sent.get('confidence'))})",
            f"- Crowd direction: {_fmt(sent.get('direction'))}",
            f"- Cross-source divergence: {'yes' if sent.get('divergence') else 'no'}",
            f"- Contrarian >=90/10 over-extension: "
            f"{'yes' if sent.get('contrarian_overextension') else 'no'}",
        ]
    else:
        lines.append("- (not supplied)")
    lines.append("")

    lines.append("## News / catalysts")
    lines.append(brief["news"] if brief.get("news") else "- (not supplied)")
    lines.append("")

    if brief.get("proposal"):
        lines += [
            "## Concrete proposal (Stage 2 input)",
            "```json",
            json.dumps(brief["proposal"], indent=2),
            "```",
            "",
        ]
    if brief.get("prior_lessons"):
        lines += [
            "## Lessons from prior decisions (trader-memory-core)",
            brief["prior_lessons"],
            "",
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Deterministic-header decision parser (free-text fallback recovery)
# --------------------------------------------------------------------------- #

# Maps a lower-cased header label to a canonical field name. Both the
# ResearchPlan ("Recommendation") and PortfolioDecision ("Rating") labels fold
# onto ``rating`` so a single parser handles either judge's output.
_FIELD_ALIASES = {
    "rating": "rating",
    "recommendation": "rating",
    "action": "action",
    "entry price": "entry_price",
    "entry": "entry_price",
    "stop loss": "stop_loss",
    "stop": "stop_loss",
    "position sizing": "position_sizing",
    "price target": "price_target",
    "time horizon": "time_horizon",
    "executive summary": "executive_summary",
    "investment thesis": "investment_thesis",
    "rationale": "rationale",
    "strategic actions": "strategic_actions",
}

# Matches a "**Header**: value" line, optionally bulleted.
_HEADER_RE = re.compile(r"^\s*[-*]?\s*\*\*(?P<key>[A-Za-z /]+?)\*\*\s*:?\s*(?P<val>.*?)\s*$")

_FLOAT_FIELDS = ("entry_price", "stop_loss", "price_target")


def parse_deterministic_headers(text: str) -> dict:
    """Recover the structured decision from a judge's deterministic-header output.

    Returns a stable record with the core decision keys (defaulting to None)
    plus a ``warnings`` list. Rating is validated against the forced 5-tier
    scale; an unrecognized rating is preserved as raw text and flagged so the
    caller never silently drops a malformed judgment.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if not m:
            continue
        label = m.group("key").strip().lower()
        canonical = _FIELD_ALIASES.get(label)
        if canonical and canonical not in fields:  # first occurrence wins
            fields[canonical] = m.group("val").strip()

    warnings: list[str] = []
    record: dict = {
        "parse_source": "deterministic_headers",
        "rating": None,
        "action": None,
        "entry_price": None,
        "stop_loss": None,
        "position_sizing": None,
        "price_target": None,
        "time_horizon": None,
        "executive_summary": None,
        "investment_thesis": None,
        "rationale": None,
        "strategic_actions": None,
        "warnings": warnings,
    }

    if "rating" in fields:
        matched = match_enum(fields["rating"], RATING_VALUES)
        if matched:
            record["rating"] = matched
        else:
            record["rating_raw"] = fields["rating"]
            warnings.append(
                f"unrecognized rating '{fields['rating']}' (not one of {'/'.join(RATING_VALUES)})"
            )
    else:
        warnings.append("no rating/recommendation header found")

    if "action" in fields:
        matched = match_enum(fields["action"], ACTION_VALUES)
        if matched:
            record["action"] = matched
        else:
            record["action_raw"] = fields["action"]
            warnings.append(f"unrecognized action '{fields['action']}'")

    for key in _FLOAT_FIELDS:
        if key in fields:
            record[key] = coerce_float(fields[key])

    for key in (
        "position_sizing",
        "time_horizon",
        "executive_summary",
        "investment_thesis",
        "rationale",
        "strategic_actions",
    ):
        if fields.get(key):
            record[key] = fields[key]

    # A long entry with a stop at/above entry is self-contradictory.
    if (
        record["entry_price"] is not None
        and record["stop_loss"] is not None
        and record["stop_loss"] >= record["entry_price"]
    ):
        warnings.append(
            f"stop_loss ({record['stop_loss']}) >= entry_price ({record['entry_price']}) "
            "for a long — verify direction/levels"
        )
    return record


def position_sizer_handoff(
    decision: dict,
    account_size: float | None = None,
    risk_pct: float = 1.0,
) -> dict:
    """Build the position-sizer hand-off from a parsed decision.

    Eligible when the decision is long-side (rating Buy/Overweight, or action
    Buy) and carries a usable entry + stop. Emits a ready-to-run
    ``position_sizer.py`` command; ``account_size`` is a placeholder when the
    caller has not supplied one.
    """
    rating = decision.get("rating")
    action = decision.get("action")
    entry = decision.get("entry_price")
    stop = decision.get("stop_loss")

    long_side = (rating in BULLISH_RATINGS) or (action in BULLISH_ACTIONS)
    if not long_side:
        return {
            "eligible": False,
            "reason": f"decision is not long-side (rating={rating}, action={action}); "
            "no long position to size",
        }
    if entry is None or stop is None:
        return {
            "eligible": False,
            "reason": "missing entry and/or stop; position-sizer needs both for a long trade",
            "entry_price": entry,
            "stop_loss": stop,
        }
    if stop >= entry:
        return {
            "eligible": False,
            "reason": f"stop_loss ({stop}) is not below entry_price ({entry}) for a long",
            "entry_price": entry,
            "stop_loss": stop,
        }

    acct = f"{account_size:g}" if account_size else "<ACCOUNT_SIZE>"
    command = (
        "python3 skills/position-sizer/scripts/position_sizer.py "
        f"--account-size {acct} --entry {entry:g} --stop {stop:g} "
        f"--risk-pct {risk_pct:g} --output-dir reports/"
    )
    return {
        "eligible": True,
        "reason": f"long-side decision (rating={rating}, action={action}) with entry+stop",
        "entry_price": entry,
        "stop_loss": stop,
        "risk_pct": risk_pct,
        "position_sizing_note": decision.get("position_sizing"),
        "suggested_command": command,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _write_reports(output_dir: str, stem: str, payload: dict, markdown: str | None):
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, f"{stem}.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    written = [json_path]
    if markdown is not None:
        md_path = os.path.join(output_dir, f"{stem}.md")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        written.append(md_path)
    return written


def cmd_assemble(args) -> int:
    valuation = summarize_dcf(load_json(args.dcf)) if args.dcf else None
    sentiment = None
    if args.sentiment:
        sentiment = summarize_sentiment(load_json(args.sentiment), args.ticker)
        if sentiment is None:
            print(
                f"warning: ticker {args.ticker} not found in {args.sentiment}",
                file=sys.stderr,
            )
    technical = None
    if args.technical:
        is_json = args.technical.lower().endswith(".json")
        technical = summarize_technical(read_text(args.technical), is_json=is_json)
    news = read_text(args.news) if args.news else None
    lessons = read_text(args.lessons) if args.lessons else None
    proposal = load_json(args.proposal) if args.proposal else None

    if not any([valuation, technical, sentiment, news]):
        print(
            "error: supply at least one analyst input (--dcf / --technical / --sentiment / --news)",
            file=sys.stderr,
        )
        return 1

    brief = build_debate_brief(
        args.ticker,
        valuation=valuation,
        technical=technical,
        sentiment=sentiment,
        news=news,
        proposal=proposal,
        prior_lessons=lessons,
    )
    markdown = render_brief_markdown(brief)
    stem = f"debate_brief_{args.ticker.upper()}_{_timestamp()}"
    for path in _write_reports(args.output_dir, stem, brief, markdown):
        print(path)
    return 0


def cmd_parse_decision(args) -> int:
    text = read_text(args.input)
    decision = parse_deterministic_headers(text)
    handoff = position_sizer_handoff(decision, args.account_size, args.risk_pct)
    payload = {
        "schema_version": "1.0",
        "source_skill": "adversarial_trade_debate",
        "stage": "final_decision",
        "decision": decision,
        "position_sizer_handoff": handoff,
    }
    for warn in decision["warnings"]:
        print(f"warning: {warn}", file=sys.stderr)
    stem = f"debate_decision_{_timestamp()}"
    for path in _write_reports(args.output_dir, stem, payload, None):
        print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic helpers for the adversarial-trade-debate skill.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    asm = sub.add_parser("assemble", help="Fold analyst outputs into a debate brief.")
    asm.add_argument("--ticker", required=True)
    asm.add_argument("--dcf", help="intrinsic-value-dcf JSON report path")
    asm.add_argument("--technical", help="technical-analyst report (.md or .json)")
    asm.add_argument("--sentiment", help="retail-sentiment-ingestor run report JSON")
    asm.add_argument("--news", help="news/catalyst text file")
    asm.add_argument("--lessons", help="prior-lessons text (trader-memory-core postmortems)")
    asm.add_argument("--proposal", help="concrete proposal JSON (action/entry/stop/size)")
    asm.add_argument("--output-dir", default="reports/")
    asm.set_defaults(func=cmd_assemble)

    dec = sub.add_parser(
        "parse-decision",
        help="Recover the structured decision from deterministic headers.",
    )
    dec.add_argument("--input", required=True, help="markdown file with judge headers")
    dec.add_argument("--account-size", type=float, default=None)
    dec.add_argument("--risk-pct", type=float, default=1.0)
    dec.add_argument("--output-dir", default="reports/")
    dec.set_defaults(func=cmd_parse_decision)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
