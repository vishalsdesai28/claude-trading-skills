#!/usr/bin/env python3
"""Morning Note Briefing assembler.

Composes the JSON outputs of several existing skills into one fixed-format,
two-minute pre-market note. This script performs **no** network I/O: it reads
the documented JSON output shapes of upstream skills and assembles them into a
ranked, PM-facing note. Fetching data is the job of the upstream skills:

    earnings-calendar          -> overnight/upcoming earnings (beats/misses/guidance)
    sector-analyst             -> sector-level daily performance / rotation regime
    economic-calendar-fetcher  -> today's macro calendar
    market-news-analyst        -> live catalysts (gdelt-news-catalyst blackout signal)
    <movers>                   -> pre-market / overnight movers (generic price list)

The note is led by the single most important development, followed by a
"Top Call" (the highest-conviction directional idea) and any actionable
long/short ideas with catalyst. All ranking is deterministic so the assembler
is fully testable offline against committed fixtures.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Priority scoring bands (0-100). Higher = leads the note. Documented in
# references/development_ranking.md.
# ---------------------------------------------------------------------------
PRIORITY_NEWS_BREAKING = 90.0
PRIORITY_NEWS_ELEVATED = 55.0
PRIORITY_NEWS_BASE = 35.0

PRIORITY_EARN_MAJOR = 80.0  # |surprise| >= 10% or guidance raised/lowered
PRIORITY_EARN_NOTABLE = 60.0  # |surprise| >= 3%
PRIORITY_EARN_MINOR = 45.0  # actual reported, small surprise
PRIORITY_EARN_SCHEDULED = 30.0  # estimate only (not yet reported)

PRIORITY_MACRO_SURPRISE = 85.0  # High impact + actual deviates from estimate
PRIORITY_MACRO_HIGH = 65.0
PRIORITY_MACRO_MEDIUM = 40.0
PRIORITY_MACRO_LOW = 20.0

PRIORITY_MOVER_LARGE = 75.0  # |move| >= 7%
PRIORITY_MOVER_NOTABLE = 55.0  # |move| >= 4%
PRIORITY_MOVER_MINOR = 35.0

PRIORITY_SECTOR_EXTREME = 55.0  # overbought/oversold present or risk-off regime
PRIORITY_SECTOR_BASE = 40.0

# Earnings / mover surprise-size thresholds (percent).
SURPRISE_MAJOR = 10.0
SURPRISE_NOTABLE = 3.0
MOVER_LARGE = 7.0
MOVER_NOTABLE = 4.0

INPUT_KEYS = ("earnings", "sector", "economic", "news", "movers")


@dataclass
class Development:
    """A single ranked development that may appear in the note."""

    category: str  # news | earnings | macro | mover | sector
    headline: str
    detail: str = ""
    priority: float = 0.0
    direction: str = "none"  # long | short | none
    ticker: str | None = None
    catalyst: str | None = None


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------
def load_json_file(path: Path | None):
    """Load a JSON file if it exists and parses; otherwise return None."""
    if path is None or not Path(path).exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
        print(f"Warning: could not load {path}: {exc}", file=sys.stderr)
        return None


def load_news_inputs(paths: list[Path] | None) -> list[dict]:
    """Load one or more news JSON files into a flat list of report dicts."""
    items: list[dict] = []
    for p in paths or []:
        data = load_json_file(p)
        if isinstance(data, list):
            items.extend(x for x in data if isinstance(x, dict))
        elif isinstance(data, dict):
            items.append(data)
    return items


def _to_float(value) -> float | None:
    """Best-effort float conversion; None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_list(data) -> list[dict]:
    """Coerce a loaded JSON payload into a list of dicts."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        # Some fetchers wrap their array under a common key.
        for key in ("earnings", "events", "data", "movers", "results"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        return [data]
    return []


# ---------------------------------------------------------------------------
# Extraction: turn each upstream shape into Development objects
# ---------------------------------------------------------------------------
def extract_news_developments(news_items: list[dict]) -> list[Development]:
    """Map market-news-analyst / gdelt-news-catalyst output to developments.

    Accepts the full ``report_to_dict`` shape (``coverage`` + ``blackout_signal``
    + ``headlines``) and the compact blackout shape
    (``{ticker, blackout, catalyst_type, severity, headline}``).
    """
    devs: list[Development] = []
    for item in news_items:
        signal = (
            item.get("blackout_signal") if isinstance(item.get("blackout_signal"), dict) else {}
        )
        coverage = item.get("coverage") if isinstance(item.get("coverage"), dict) else {}

        ticker = item.get("ticker") or signal.get("ticker")
        blackout = bool(item.get("blackout") or signal.get("blackout") or coverage.get("breaking"))
        severity = (
            item.get("severity")
            or signal.get("severity")
            or coverage.get("severity")
            or ("high" if blackout else "none")
        )
        severity = str(severity).lower()
        elevated = severity == "elevated" or bool(coverage.get("elevated"))

        headline = (
            item.get("headline")
            or signal.get("top_headline")
            or (item.get("headlines") or [{}])[0].get("title")
            or (f"{ticker} " if ticker else "") + f"coverage {severity}"
        ).strip()

        if blackout or severity in ("high", "critical"):
            priority = PRIORITY_NEWS_BREAKING
        elif elevated:
            priority = PRIORITY_NEWS_ELEVATED
        else:
            priority = PRIORITY_NEWS_BASE

        catalyst = item.get("catalyst_type") or item.get("catalyst") or "news coverage surge"
        surge_x = coverage.get("surge_x") or signal.get("surge_x")
        if surge_x:
            detail = f"Coverage {surge_x}x baseline."
        elif severity in ("none", ""):
            detail = "No coverage surge."
        else:
            detail = f"{severity.capitalize()} coverage."

        devs.append(
            Development(
                category="news",
                headline=headline,
                detail=detail,
                priority=priority,
                direction="none",  # attention, not direction
                ticker=ticker,
                catalyst=catalyst,
            )
        )
    return devs


def earnings_surprise_pct(rec: dict) -> float | None:
    """Percent EPS surprise from actual vs estimate; None if either missing."""
    actual = _to_float(rec.get("epsActual") or rec.get("eps"))
    estimate = _to_float(rec.get("epsEstimated") or rec.get("epsEstimate"))
    if actual is None or estimate is None or estimate == 0:
        # Allow a caller-supplied surprise field as a fallback.
        return _to_float(rec.get("epsSurprisePct") or rec.get("surprisePct"))
    return round((actual - estimate) / abs(estimate) * 100.0, 1)


def _earnings_direction(surprise: float | None, guidance: str | None) -> str:
    """Guidance dominates; otherwise sign of a material surprise sets direction."""
    g = (guidance or "").lower()
    if g == "lowered":
        return "short"
    if g == "raised":
        return "long"
    if surprise is not None:
        if surprise >= SURPRISE_NOTABLE:
            return "long"
        if surprise <= -SURPRISE_NOTABLE:
            return "short"
    return "none"


def extract_earnings_developments(earnings: list[dict]) -> list[Development]:
    """Map earnings-calendar records to developments.

    earnings-calendar is forward-looking (estimates); when the analyst enriches
    a record with ``epsActual`` and/or ``guidance``, the beat/miss and direction
    are computed. Records with estimates only are surfaced as scheduled events.
    """
    devs: list[Development] = []
    for rec in earnings:
        symbol = rec.get("symbol") or rec.get("ticker")
        name = rec.get("companyName") or symbol or "Company"
        surprise = earnings_surprise_pct(rec)
        guidance = rec.get("guidance")
        has_actual = (
            _to_float(rec.get("epsActual") or rec.get("eps")) is not None or surprise is not None
        )
        g = (guidance or "").lower()

        if (surprise is not None and abs(surprise) >= SURPRISE_MAJOR) or g in ("raised", "lowered"):
            priority = PRIORITY_EARN_MAJOR
        elif surprise is not None and abs(surprise) >= SURPRISE_NOTABLE:
            priority = PRIORITY_EARN_NOTABLE
        elif has_actual:
            priority = PRIORITY_EARN_MINOR
        else:
            priority = PRIORITY_EARN_SCHEDULED

        if surprise is None:
            verb = "reports" if has_actual else "reports (scheduled)"
            headline = f"{name} ({symbol}) {verb}"
        else:
            beat = "beat" if surprise > 0 else ("miss" if surprise < 0 else "in-line")
            headline = f"{name} ({symbol}) EPS {beat} {surprise:+.1f}% vs est"

        detail_bits = []
        if guidance:
            detail_bits.append(f"Guidance {guidance}.")
        reaction = _to_float(rec.get("reaction") or rec.get("pctChange"))
        if reaction is not None:
            detail_bits.append(f"Reaction {reaction:+.1f}%.")
        if rec.get("timing"):
            detail_bits.append(f"Timing {rec['timing']}.")

        devs.append(
            Development(
                category="earnings",
                headline=headline,
                detail=" ".join(detail_bits),
                priority=priority,
                direction=_earnings_direction(surprise, guidance),
                ticker=symbol,
                catalyst="earnings" + (f" ({guidance} guidance)" if guidance else ""),
            )
        )
    return devs


def extract_macro_developments(econ: list[dict], as_of: str) -> list[Development]:
    """Map economic-calendar-fetcher events dated ``as_of`` to developments."""
    devs: list[Development] = []
    for ev in econ:
        ev_date = str(ev.get("date", ""))
        if as_of and not ev_date.startswith(as_of):
            continue
        impact = str(ev.get("impact", "")).lower()
        actual = ev.get("actual")
        estimate = ev.get("estimate")
        has_surprise = actual is not None and estimate is not None

        if impact == "high" and has_surprise:
            priority = PRIORITY_MACRO_SURPRISE
        elif impact == "high":
            priority = PRIORITY_MACRO_HIGH
        elif impact == "medium":
            priority = PRIORITY_MACRO_MEDIUM
        else:
            priority = PRIORITY_MACRO_LOW

        name = ev.get("event", "Economic event")
        country = ev.get("country", "")
        headline = f"{name}{f' ({country})' if country else ''}"
        detail_bits = []
        if estimate is not None:
            detail_bits.append(f"est {estimate}")
        if ev.get("previous") is not None:
            detail_bits.append(f"prev {ev['previous']}")
        if actual is not None:
            detail_bits.append(f"actual {actual}")
        detail = f"Impact {impact or 'n/a'}. " + (", ".join(detail_bits) if detail_bits else "")

        devs.append(
            Development(
                category="macro",
                headline=headline,
                detail=detail.strip(),
                priority=priority,
                direction="none",
                catalyst="macro data",
            )
        )
    return devs


def extract_mover_developments(movers: list[dict]) -> list[Development]:
    """Map a generic pre-market / overnight movers list to developments.

    Expected shape per row: ``{ticker, name?, pct_change, price?, catalyst?,
    session?}``. Positive move => long candidate, negative => short candidate.
    """
    devs: list[Development] = []
    for m in movers:
        ticker = m.get("ticker") or m.get("symbol")
        pct = _to_float(m.get("pct_change") or m.get("pctChange") or m.get("change_pct"))
        if pct is None:
            continue
        absmove = abs(pct)
        if absmove >= MOVER_LARGE:
            priority = PRIORITY_MOVER_LARGE
        elif absmove >= MOVER_NOTABLE:
            priority = PRIORITY_MOVER_NOTABLE
        else:
            priority = PRIORITY_MOVER_MINOR

        session = m.get("session") or "pre-market"
        headline = f"{ticker} {pct:+.1f}% {session}"
        catalyst = m.get("catalyst") or "price/volume move"
        detail_bits = []
        if m.get("price") is not None:
            detail_bits.append(f"@ {m['price']}")
        if m.get("name"):
            detail_bits.append(str(m["name"]))
        devs.append(
            Development(
                category="mover",
                headline=headline,
                detail=" ".join(detail_bits),
                priority=priority,
                direction="long" if pct > 0 else "short",
                ticker=ticker,
                catalyst=catalyst,
            )
        )
    return devs


def extract_sector_development(sector_data: dict | None) -> Development | None:
    """Map sector-analyst JSON to a single sector-read development."""
    if not isinstance(sector_data, dict):
        return None
    groups = sector_data.get("groups") if isinstance(sector_data.get("groups"), dict) else {}
    regime = str(groups.get("regime", "")).strip()
    score = groups.get("score")
    ranking = sector_data.get("ranking") if isinstance(sector_data.get("ranking"), list) else []
    leaders = [r.get("sector") for r in ranking[:2] if isinstance(r, dict) and r.get("sector")]
    overbought = sector_data.get("overbought") or []
    oversold = sector_data.get("oversold") or []
    cycle = (
        sector_data.get("cycle_phase") if isinstance(sector_data.get("cycle_phase"), dict) else {}
    )

    if not regime and not leaders:
        return None

    risk_off = "risk off" in regime.lower() or "defensive" in regime.lower()
    priority = (
        PRIORITY_SECTOR_EXTREME if (overbought or oversold or risk_off) else PRIORITY_SECTOR_BASE
    )

    headline_bits = []
    if regime:
        headline_bits.append(f"{regime} regime")
    if score is not None:
        headline_bits.append(f"(score {score})")
    if leaders:
        headline_bits.append("— leaders " + ", ".join(leaders))
    headline = "Sector rotation: " + " ".join(headline_bits)

    detail_bits = []
    phase = cycle.get("phase") or cycle.get("cycle_phase")
    if phase:
        detail_bits.append(f"Cycle phase: {phase}.")
    if overbought:
        names = ", ".join(x.get("sector", "") for x in overbought if isinstance(x, dict))
        detail_bits.append(f"Overbought: {names}.")
    if oversold:
        names = ", ".join(x.get("sector", "") for x in oversold if isinstance(x, dict))
        detail_bits.append(f"Oversold: {names}.")

    return Development(
        category="sector",
        headline=headline,
        detail=" ".join(detail_bits),
        priority=priority,
        direction="none",
        catalyst="sector rotation",
    )


# ---------------------------------------------------------------------------
# Ranking + selection
# ---------------------------------------------------------------------------
def rank_developments(devs: list[Development]) -> list[Development]:
    """Stable sort by priority descending (insertion order breaks ties)."""
    return sorted(devs, key=lambda d: -d.priority)


def select_lead(ranked: list[Development]) -> Development | None:
    """The single most important development leads the note."""
    return ranked[0] if ranked else None


def select_top_call(ranked: list[Development]) -> Development | None:
    """Highest-priority development that carries a directional (long/short) view."""
    for d in ranked:
        if d.direction in ("long", "short"):
            return d
    return None


def build_actionable_ideas(ranked: list[Development], max_ideas: int = 4) -> list[dict]:
    """Distinct long/short ideas (one per ticker) with thesis, catalyst, risk."""
    ideas: list[dict] = []
    seen: set[str] = set()
    for d in ranked:
        if d.direction not in ("long", "short") or not d.ticker:
            continue
        if d.ticker in seen:
            continue
        seen.add(d.ticker)
        risk = (
            "Gap fades on open; confirm follow-through before sizing."
            if d.direction == "long"
            else "Squeeze risk into strength; require a lower-high before pressing."
        )
        ideas.append(
            {
                "direction": d.direction.upper(),
                "ticker": d.ticker,
                "thesis": d.headline,
                "catalyst": d.catalyst or "n/a",
                "risk": risk,
            }
        )
        if len(ideas) >= max_ideas:
            break
    return ideas


def dev_to_dict(dev: Development) -> dict:
    return asdict(dev)


# ---------------------------------------------------------------------------
# Assembly + rendering
# ---------------------------------------------------------------------------
@dataclass
class NoteInputs:
    earnings: list[dict] = field(default_factory=list)
    sector: dict | None = None
    economic: list[dict] = field(default_factory=list)
    news: list[dict] = field(default_factory=list)
    movers: list[dict] = field(default_factory=list)


def assemble_note(
    inputs: NoteInputs,
    *,
    as_of: str,
    analyst: str = "Desk",
    coverage: str = "US Equities",
    generated_at: str | None = None,
) -> dict:
    """Compose upstream JSON into the structured morning-note object."""
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).isoformat()

    devs: list[Development] = []
    devs += extract_news_developments(inputs.news)
    devs += extract_earnings_developments(inputs.earnings)
    macro_devs = extract_macro_developments(inputs.economic, as_of)
    devs += macro_devs
    devs += extract_mover_developments(inputs.movers)
    sector_dev = extract_sector_development(inputs.sector)
    if sector_dev is not None:
        devs.append(sector_dev)

    ranked = rank_developments(devs)
    lead = select_lead(ranked)
    top_call = select_top_call(ranked)
    ideas = build_actionable_ideas(ranked)

    provided = [k for k in INPUT_KEYS if _input_present(inputs, k)]
    missing = [k for k in INPUT_KEYS if k not in provided]

    return {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "as_of": as_of,
        "analyst": analyst,
        "coverage": coverage,
        "lead": dev_to_dict(lead) if lead else None,
        "top_call": dev_to_dict(top_call) if top_call else None,
        "developments": [dev_to_dict(d) for d in ranked],
        "macro_today": [dev_to_dict(d) for d in rank_developments(macro_devs)],
        "actionable_ideas": ideas,
        "sector_read": (
            sector_dev.headline + (" " + sector_dev.detail if sector_dev.detail else "")
        )
        if sector_dev
        else None,
        "inputs_provided": provided,
        "inputs_missing": missing,
    }


def _input_present(inputs: NoteInputs, key: str) -> bool:
    value = getattr(inputs, key)
    return bool(value)


def render_markdown(note: dict) -> str:
    """Render the fixed-format, two-minute PM note (see references/note_template.md)."""
    lines: list[str] = []
    lines.append(f"# {note['as_of']} Morning Note")
    lines.append(
        f"**Prepared:** {note['generated_at']} | "
        f"**Coverage:** {note['coverage']} | **Analyst:** {note['analyst']}"
    )
    lines.append("")

    lead = note.get("lead")
    if lead:
        lines.append(f"## Lead: {lead['headline']}")
        if lead.get("detail"):
            lines.append(lead["detail"])
    else:
        lines.append("## Lead: Nothing material overnight")
        lines.append("No market-moving development in coverage; maintain current positioning.")
    lines.append("")

    lines.append("## Top Call")
    top = note.get("top_call")
    if top:
        arrow = "LONG" if top["direction"] == "long" else "SHORT"
        ticker = f" {top['ticker']}" if top.get("ticker") else ""
        lines.append(f"**{arrow}{ticker}** — {top['headline']}")
        lines.append(f"Catalyst: {top.get('catalyst') or 'n/a'}.")
    else:
        lines.append("No high-conviction directional call; stay reactive to the tape.")
    lines.append("")

    ideas = note.get("actionable_ideas") or []
    if ideas:
        lines.append("## Actionable Ideas")
        for idea in ideas:
            lines.append(
                f"- **{idea['direction']} {idea['ticker']}**: {idea['thesis']} "
                f"— catalyst: {idea['catalyst']} | risk: {idea['risk']}"
            )
        lines.append("")

    devs = [
        d for d in note.get("developments", []) if d["category"] in ("news", "earnings", "mover")
    ]
    if devs:
        lines.append("## Overnight & Pre-Market Developments")
        for d in devs[:8]:
            tag = f"[{d['ticker']}] " if d.get("ticker") else ""
            detail = f" — {d['detail']}" if d.get("detail") else ""
            lines.append(f"- {tag}{d['headline']}{detail}")
        lines.append("")

    macro = note.get("macro_today") or []
    if macro:
        lines.append("## Macro Calendar — Today")
        for d in macro:
            detail = f" — {d['detail']}" if d.get("detail") else ""
            lines.append(f"- {d['headline']}{detail}")
        lines.append("")

    if note.get("sector_read"):
        lines.append("## Sector Read")
        lines.append(note["sector_read"])
        lines.append("")

    lines.append("---")
    lines.append(
        "*Inputs: earnings-calendar, sector-analyst, economic-calendar-fetcher, "
        "market-news-analyst. Estimates only — pre-market moves may change by open.*"
    )
    if note.get("inputs_missing"):
        lines.append(f"*Missing inputs: {', '.join(note['inputs_missing'])}.*")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_inputs_from_args(args: argparse.Namespace) -> NoteInputs:
    return NoteInputs(
        earnings=_as_list(load_json_file(args.earnings)),
        sector=load_json_file(args.sector) if args.sector else None,
        economic=_as_list(load_json_file(args.economic)),
        news=load_news_inputs(args.news),
        movers=_as_list(load_json_file(args.movers)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble a fixed-format morning note from upstream skill JSON outputs"
    )
    parser.add_argument("--earnings", type=Path, help="earnings-calendar JSON (array)")
    parser.add_argument("--sector", type=Path, help="sector-analyst JSON")
    parser.add_argument("--economic", type=Path, help="economic-calendar-fetcher JSON (array)")
    parser.add_argument(
        "--news", type=Path, nargs="+", help="one or more market-news-analyst / gdelt JSON files"
    )
    parser.add_argument("--movers", type=Path, help="pre-market/overnight movers JSON (array)")
    parser.add_argument("--as-of", default=date.today().isoformat(), help="note date (YYYY-MM-DD)")
    parser.add_argument("--analyst", default="Desk", help="analyst / desk name")
    parser.add_argument("--coverage", default="US Equities", help="coverage universe label")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports"),
        help="output directory (default: reports/)",
    )
    parser.add_argument("--json-only", action="store_true", help="skip the markdown file")
    args = parser.parse_args(argv)

    inputs = build_inputs_from_args(args)
    note = assemble_note(
        inputs,
        as_of=args.as_of,
        analyst=args.analyst,
        coverage=args.coverage,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"morning_note_{args.as_of}.json"
    with open(json_path, "w") as f:
        json.dump(note, f, indent=2)
    print(f"JSON note: {json_path}")

    if not args.json_only:
        md_path = args.output_dir / f"morning_note_{args.as_of}.md"
        with open(md_path, "w") as f:
            f.write(render_markdown(note))
        print(f"Markdown note: {md_path}")

    lead = note.get("lead")
    print(f"\nLead: {lead['headline'] if lead else 'nothing material overnight'}")
    top = note.get("top_call")
    if top:
        print(f"Top Call: {top['direction'].upper()} {top.get('ticker') or ''} — {top['headline']}")
    print(f"Actionable ideas: {len(note.get('actionable_ideas') or [])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
