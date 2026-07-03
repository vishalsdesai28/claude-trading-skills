#!/usr/bin/env python3
"""finviz-screener: keyless yfinance multi-factor boolean screener.

A free alternative to the FINVIZ Elite / FMP paid screeners. Composes nested
AND/OR boolean filter trees over Yahoo Finance screener fields and runs them
keyless via yfinance (no API key). Also exposes yfinance predefined screens
and a keyword ticker + news search. Emits a results table (markdown + JSON)
to reports/.

A query is supplied either as JSON (matching the documented spec shape) or as
a small DSL, e.g.::

    "intradaymarketcap gte 2e9 and (percentchange gt 3 or forward_dividend_yield gte 0.03)"

Leaf operators: gt lt gte lte eq btwn is-in (aliases: > < >= <= == in between).
Boolean operators: and or (AND binds tighter than OR; group with parentheses).

Spec (JSON) shape — flat operand lists, identical to what yfinance expects::

    {"operator": "and", "operands": [
        {"operator": "gt",    "operands": ["intradaymarketcap", 2000000000]},
        {"operator": "btwn",  "operands": ["percentchange", 1, 5]},
        {"operator": "is-in", "operands": ["sector", "Technology", "Healthcare"]}
    ]}

yfinance is imported lazily *inside* the fetch functions so the query builder,
DSL parser and table formatter import (and unit-test) with the stdlib only,
against committed fixtures and fully offline.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import numbers
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Operator vocabulary
# ---------------------------------------------------------------------------

LEAF_OPS: frozenset[str] = frozenset({"gt", "lt", "gte", "lte", "eq", "btwn", "is-in"})
BOOL_OPS: frozenset[str] = frozenset({"and", "or"})

# Human-friendly aliases → canonical operator.
_OP_ALIASES: dict[str, str] = {
    ">": "gt",
    "<": "lt",
    ">=": "gte",
    "<=": "lte",
    "=": "eq",
    "==": "eq",
    "in": "is-in",
    "isin": "is-in",
    "is_in": "is-in",
    "is-in": "is-in",
    "between": "btwn",
    "btwn": "btwn",
    "gt": "gt",
    "lt": "lt",
    "gte": "gte",
    "lte": "lte",
    "eq": "eq",
    "and": "and",
    "or": "or",
}

# Columns pulled from each Yahoo screener quote for the results table.
DEFAULT_COLUMNS: list[str] = [
    "symbol",
    "shortName",
    "regularMarketPrice",
    "regularMarketChangePercent",
    "marketCap",
    "trailingPE",
    "fiftyTwoWeekChangePercent",
    "averageDailyVolume3Month",
    "fullExchangeName",
]

MAX_COUNT = 250


# ---------------------------------------------------------------------------
# Pure: operator + value helpers
# ---------------------------------------------------------------------------


def normalize_operator(op: Any) -> str:
    """Return the canonical lowercase operator for *op* (accepts aliases).

    Raises:
        ValueError: If *op* is not a recognized operator or alias.
    """
    if not isinstance(op, str):
        raise ValueError(f"Operator must be a string, got {type(op).__name__}")
    canon = _OP_ALIASES.get(op.strip().lower())
    if canon is None:
        raise ValueError(
            f"Unknown operator '{op}'. Leaf: {sorted(LEAF_OPS)}; boolean: {sorted(BOOL_OPS)}."
        )
    return canon


def coerce_value(token: str) -> Any:
    """Coerce a DSL value token to int/float when numeric, else keep the string."""
    tok = token.strip()
    if re.fullmatch(r"-?\d+", tok):
        return int(tok)
    try:
        return float(tok)
    except ValueError:
        return tok


def _is_number(value: Any) -> bool:
    """True for real numbers, excluding bool (which is an int subclass)."""
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Pure: spec validation / normalization (recursive)
# ---------------------------------------------------------------------------


def normalize_spec(spec: Any) -> dict[str, Any]:
    """Validate a filter spec and return a normalized copy (operators canonical).

    Validates operator names, operand arity and operand types for the whole
    nested tree so a malformed query fails fast — before any network call.

    Raises:
        ValueError: On any structural or type problem in the tree.
    """
    if not isinstance(spec, dict) or "operator" not in spec or "operands" not in spec:
        raise ValueError("Each filter node must be a dict with 'operator' and 'operands'.")

    op = normalize_operator(spec["operator"])
    operands = spec["operands"]
    if not isinstance(operands, list) or not operands:
        raise ValueError(f"Operands for '{op}' must be a non-empty list.")

    if op in BOOL_OPS:
        if len(operands) < 2:
            raise ValueError(f"Boolean '{op}' needs at least 2 sub-filters.")
        return {"operator": op, "operands": [normalize_spec(sub) for sub in operands]}

    # Leaf node: operands[0] is the field name, remainder are comparison values.
    field = operands[0]
    if not isinstance(field, str) or not field.strip():
        raise ValueError(f"Leaf '{op}' first operand must be a non-empty field name.")

    if op in ("gt", "lt", "gte", "lte", "eq"):
        if len(operands) != 2:
            raise ValueError(f"'{op}' takes exactly [field, value] (got {len(operands)}).")
        if op != "eq" and not _is_number(operands[1]):
            raise ValueError(f"'{op}' comparison value must be numeric, got {operands[1]!r}.")
    elif op == "btwn":
        if len(operands) != 3:
            raise ValueError("'btwn' takes exactly [field, low, high].")
        if not (_is_number(operands[1]) and _is_number(operands[2])):
            raise ValueError("'btwn' bounds must both be numeric.")
    elif op == "is-in":
        if len(operands) < 2:
            raise ValueError("'is-in' takes [field, value, ...] with at least one value.")

    return {"operator": op, "operands": list(operands)}


# ---------------------------------------------------------------------------
# Pure: DSL parser (recursive descent, AND binds tighter than OR)
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """Split a DSL string into parentheses and whitespace-delimited words."""
    return re.findall(r"\(|\)|[^\s()]+", text)


class _DslParser:
    """Recursive-descent parser producing a normalized filter spec dict."""

    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> str:
        if self.pos >= len(self.tokens):
            raise ValueError("Unexpected end of query.")
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> dict[str, Any]:
        node = self._parse_or()
        if self.pos != len(self.tokens):
            raise ValueError(f"Unexpected token '{self.tokens[self.pos]}' in query.")
        return node

    def _parse_or(self) -> dict[str, Any]:
        terms = [self._parse_and()]
        while (self._peek() or "").lower() == "or":
            self._next()
            terms.append(self._parse_and())
        return terms[0] if len(terms) == 1 else {"operator": "or", "operands": terms}

    def _parse_and(self) -> dict[str, Any]:
        terms = [self._parse_term()]
        while (self._peek() or "").lower() == "and":
            self._next()
            terms.append(self._parse_term())
        return terms[0] if len(terms) == 1 else {"operator": "and", "operands": terms}

    def _parse_term(self) -> dict[str, Any]:
        if self._peek() == "(":
            self._next()
            node = self._parse_or()
            if self._peek() != ")":
                raise ValueError("Unbalanced parentheses in query.")
            self._next()
            return node
        return self._parse_leaf()

    def _parse_leaf(self) -> dict[str, Any]:
        field = self._next()
        if field in ("(", ")") or field.lower() in BOOL_OPS:
            raise ValueError(f"Expected a field name, got '{field}'.")
        op = normalize_operator(self._next())
        if op == "btwn":
            operands = [field, coerce_value(self._next()), coerce_value(self._next())]
        elif op == "is-in":
            values = [v.strip() for v in self._next().split(",") if v.strip()]
            if not values:
                raise ValueError(f"'is-in' on '{field}' needs a comma-separated value list.")
            operands = [field, *values]
        else:
            operands = [field, coerce_value(self._next())]
        return {"operator": op, "operands": operands}


def parse_dsl(text: str) -> dict[str, Any]:
    """Parse the small boolean DSL into a normalized filter spec dict."""
    tokens = _tokenize(text or "")
    if not tokens:
        raise ValueError("Empty query.")
    spec = _DslParser(tokens).parse()
    return normalize_spec(spec)


def load_spec(*, dsl: str | None, query_json: str | None, query_file: str | None) -> dict[str, Any]:
    """Resolve exactly one of dsl / query_json / query_file into a normalized spec."""
    if dsl is not None:
        return parse_dsl(dsl)
    if query_json is not None:
        return normalize_spec(json.loads(query_json))
    if query_file is not None:
        return normalize_spec(json.loads(Path(query_file).read_text()))
    raise ValueError("No query provided.")


# ---------------------------------------------------------------------------
# Pure: result shaping + markdown table
# ---------------------------------------------------------------------------


def screen_result_to_rows(
    result: dict[str, Any] | None, columns: list[str] | None = None
) -> list[dict[str, Any]]:
    """Extract the requested columns from a yf.screen result into plain rows."""
    cols = columns or DEFAULT_COLUMNS
    quotes = result.get("quotes", []) if isinstance(result, dict) else []
    rows: list[dict[str, Any]] = []
    for q in quotes:
        if not isinstance(q, dict):
            continue
        row = {c: q.get(c) for c in cols}
        # Fall back to longName when shortName is absent so the name cell isn't blank.
        if "shortName" in cols and not row.get("shortName"):
            row["shortName"] = q.get("longName")
        rows.append(row)
    return rows


def _humanize_marketcap(value: float) -> str:
    for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(value) >= size:
            return f"{value / size:.2f}{unit}"
    return f"{value:,.0f}"


def _fmt_cell(column: str, value: Any) -> str:
    """Render one cell, humanizing caps/volumes/percentages; escape pipes."""
    if value is None:
        return "-"
    low = column.lower()
    if _is_number(value):
        if "marketcap" in low:
            return _humanize_marketcap(float(value))
        if "volume" in low:
            return f"{int(value):,}"
        if low.endswith("percent") or low.endswith("changepercent"):
            return f"{float(value):.2f}%"
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)
    return str(value).replace("|", "\\|")


def rows_to_markdown(
    rows: list[dict[str, Any]],
    columns: list[str] | None = None,
    *,
    title: str = "Keyless yfinance Screen",
    subtitle: str | None = None,
    total: int | None = None,
) -> str:
    """Render rows as a markdown document with a header and results table."""
    cols = columns or DEFAULT_COLUMNS
    lines = [f"# {title}", ""]
    if subtitle:
        lines += [subtitle, ""]
    lines.append(f"**Results shown:** {len(rows)}")
    if total is not None:
        lines.append(f"**Total matches:** {total:,}")
    lines.append("")

    if not rows:
        lines.append("_No matching stocks._")
        return "\n".join(lines) + "\n"

    lines.append("| " + " | ".join(cols) + " |")
    lines.append("| " + " | ".join(["---"] * len(cols)) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_fmt_cell(c, row.get(c)) for c in cols) + " |")
    return "\n".join(lines) + "\n"


def build_payload(
    *,
    mode: str,
    query: Any,
    rows: list[dict[str, Any]],
    columns: list[str],
    total: int | None,
    sort_field: str | None,
    sort_asc: bool,
    now: dt.datetime,
) -> dict[str, Any]:
    """Assemble the JSON payload written alongside the markdown report."""
    return {
        "generated_at": now.isoformat(),
        "source": "yfinance",
        "mode": mode,
        "query": query,
        "sort": {"field": sort_field, "ascending": sort_asc},
        "columns": columns,
        "result_count": len(rows),
        "total_matches": total,
        "rows": rows,
    }


def write_reports(
    out_dir: Path, prefix: str, now: dt.datetime, markdown: str, payload: dict[str, Any]
) -> tuple[Path, Path]:
    """Write the markdown + JSON reports and return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    md_path = out_dir / f"{prefix}_{stamp}.md"
    json_path = out_dir / f"{prefix}_{stamp}.json"
    md_path.write_text(markdown)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return md_path, json_path


# ---------------------------------------------------------------------------
# Lazy yfinance (network) — imported inside each fetcher; monkeypatched in tests
# ---------------------------------------------------------------------------


def build_equity_query(spec: dict[str, Any]):
    """Recursively construct a yfinance EquityQuery from a normalized spec."""
    import yfinance as yf  # lazy

    op = spec["operator"]
    operands = spec["operands"]
    if op in BOOL_OPS:
        return yf.EquityQuery(op.upper(), [build_equity_query(s) for s in operands])
    return yf.EquityQuery(op.upper(), list(operands))


def run_boolean_screen(
    spec: dict[str, Any], sort_field: str, sort_asc: bool, count: int
) -> dict[str, Any]:
    """Run a custom boolean screen keyless via yfinance."""
    import yfinance as yf  # lazy

    query = build_equity_query(spec)
    return yf.screen(query, sortField=sort_field, sortAsc=sort_asc, size=count)


def list_predefined() -> list[str]:
    """Return the sorted list of yfinance predefined screen names."""
    import yfinance as yf  # lazy

    return sorted(yf.PREDEFINED_SCREENER_QUERIES)


def run_predefined_screen(name: str, count: int) -> dict[str, Any]:
    """Run a yfinance predefined screen by name."""
    import yfinance as yf  # lazy

    if name not in yf.PREDEFINED_SCREENER_QUERIES:
        raise ValueError(
            f"Unknown predefined screen '{name}'. "
            f"Available: {sorted(yf.PREDEFINED_SCREENER_QUERIES)}"
        )
    return yf.screen(name, count=count)


def search_tickers(query: str, max_results: int = 10, news_count: int = 5) -> dict[str, Any]:
    """Keyword search for tickers and related news via yfinance."""
    import yfinance as yf  # lazy

    s = yf.Search(query, max_results=max_results, news_count=news_count)
    return {"quotes": list(s.quotes), "news": list(s.news)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_columns(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    cols = [c.strip() for c in raw.split(",") if c.strip()]
    return cols or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Keyless yfinance multi-factor boolean screener (no API key).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dsl", help="Boolean DSL query, e.g. 'intradaymarketcap gte 2e9 and percentchange gt 3'."
    )
    mode.add_argument("--query-json", help="Filter spec as a JSON string.")
    mode.add_argument("--query-file", help="Path to a JSON file holding the filter spec.")
    mode.add_argument("--predefined", help="Run a yfinance predefined screen by name.")
    mode.add_argument("--search", help="Keyword ticker + news search.")
    mode.add_argument(
        "--list-predefined", action="store_true", help="List predefined screen names and exit."
    )

    parser.add_argument(
        "--sort-field", default="intradaymarketcap", help="Sort field (default: intradaymarketcap)."
    )
    parser.add_argument(
        "--sort-asc", action="store_true", help="Sort ascending (default: descending)."
    )
    parser.add_argument(
        "--count", type=int, default=25, help="Max results, capped at 250 (default: 25)."
    )
    parser.add_argument(
        "--columns", default=None, help="Comma-separated output columns (default: a curated set)."
    )
    parser.add_argument(
        "--news-count", type=int, default=5, help="News articles for --search (default: 5)."
    )
    parser.add_argument("--title", default=None, help="Report title override.")
    parser.add_argument(
        "--output-dir", default="reports/", help="Output directory (default: reports/)."
    )
    parser.add_argument("--output-prefix", default="yf_screen", help="Output filename prefix.")
    parser.add_argument(
        "--no-report", action="store_true", help="Skip writing report files; print summary only."
    )
    return parser.parse_args(argv)


def _emit(summary: dict[str, Any]) -> None:
    print(json.dumps(summary, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    count = max(1, min(args.count, MAX_COUNT))
    columns = parse_columns(args.columns) or DEFAULT_COLUMNS

    # --- list predefined: no network beyond the yfinance registry ---
    if args.list_predefined:
        _emit({"predefined_screens": list_predefined()})
        return 0

    # --- keyword search ---
    if args.search is not None:
        try:
            result = search_tickers(args.search, max_results=count, news_count=args.news_count)
        except Exception as exc:  # noqa: BLE001 — surface any yfinance/network failure cleanly
            print(f"Error: ticker search failed: {exc}", file=sys.stderr)
            return 1
        summary = {
            "mode": "search",
            "query": args.search,
            "quotes": result["quotes"],
            "news": result["news"],
        }
        if not args.no_report:
            md = "# Ticker + News Search: " + args.search + "\n\n"
            md += "## Tickers\n\n"
            if result["quotes"]:
                md += "| symbol | name | exchange | sector | industry |\n| --- | --- | --- | --- | --- |\n"
                for q in result["quotes"]:
                    name = q.get("shortname") or q.get("longname") or ""
                    md += (
                        f"| {q.get('symbol', '-')} | {name} | {q.get('exchDisp', '-')} "
                        f"| {q.get('sector', '-')} | {q.get('industry', '-')} |\n"
                    )
            else:
                md += "_No tickers found._\n"
            md += "\n## News\n\n"
            for n in result["news"]:
                md += f"- [{n.get('title', 'untitled')}]({n.get('link', '')}) — {n.get('publisher', '')}\n"
            md_path, json_path = write_reports(
                Path(args.output_dir), args.output_prefix + "_search", now, md, summary
            )
            summary["report_markdown"] = str(md_path)
            summary["report_json"] = str(json_path)
        _emit(summary)
        return 0

    # --- screens (predefined or boolean) ---
    try:
        if args.predefined is not None:
            mode = "predefined"
            query: Any = args.predefined
            result = run_predefined_screen(args.predefined, count)
        else:
            mode = "boolean"
            query = load_spec(dsl=args.dsl, query_json=args.query_json, query_file=args.query_file)
            result = run_boolean_screen(query, args.sort_field, args.sort_asc, count)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — surface any yfinance/network failure cleanly
        print(f"Error: screen failed: {exc}", file=sys.stderr)
        return 1

    total = result.get("total") if isinstance(result, dict) else None
    rows = screen_result_to_rows(result, columns)
    payload = build_payload(
        mode=mode,
        query=query,
        rows=rows,
        columns=columns,
        total=total,
        sort_field=args.sort_field,
        sort_asc=args.sort_asc,
        now=now,
    )
    summary: dict[str, Any] = {
        "mode": mode,
        "result_count": len(rows),
        "total_matches": total,
        "symbols": [r.get("symbol") for r in rows],
    }
    if not args.no_report:
        title = args.title or (
            f"Predefined Screen: {args.predefined}"
            if mode == "predefined"
            else "Keyless yfinance Screen"
        )
        subtitle = None if mode == "predefined" else f"`{json.dumps(query)}`"
        md = rows_to_markdown(rows, columns, title=title, subtitle=subtitle, total=total)
        md_path, json_path = write_reports(
            Path(args.output_dir), args.output_prefix, now, md, payload
        )
        summary["report_markdown"] = str(md_path)
        summary["report_json"] = str(json_path)
    _emit(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
