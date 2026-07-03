"""Trader Memory Core — alpha-attribution reflection log.

Append-only markdown log of thesis outcomes and terse reflections, with a
``pending -> resolved`` lifecycle, atomic temp-file writes, and idempotency
guards. When a thesis is closed the postmortem path writes a *pending* entry
(the decision), then *resolves* it with the raw return, the alpha vs the
benchmark, the holding period, and a 2-4 sentence reflection.

``get_past_context()`` reads the resolved entries back into a compact block
suitable for injection into a future analysis prompt (e.g. the
adversarial-trade-debate): N most-recent same-ticker *full* entries plus N
cross-ticker *reflection-only* lessons.

Stdlib-only on purpose (no yaml / jsonschema): the log is plain markdown and
must stay writable even from a bare interpreter. The lifecycle is adapted in
spirit from TradingAgents' decision-memory log and re-implemented here.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# HTML comment: cannot appear in reflection prose, safe as a hard delimiter.
SEPARATOR = "\n\n<!-- ENTRY_END -->\n\n"

_DECISION_RE = re.compile(r"DECISION:\n(.*?)(?=\nREFLECTION:|\Z)", re.DOTALL)
_REFLECTION_RE = re.compile(r"REFLECTION:\n(.*?)$", re.DOTALL)


# -- Formatting helpers -------------------------------------------------------


def _fmt_pct(value: float | None) -> str:
    """Signed one-decimal percent, or ``n/a`` when unknown."""
    return "n/a" if value is None else f"{value:+.1f}%"


def _tag_thesis_id(tag_line: str) -> str | None:
    """Return the thesis_id field from a ``[...]`` tag line, or None."""
    if not (tag_line.startswith("[") and tag_line.endswith("]")):
        return None
    return tag_line[1:-1].split("|")[0].strip()


# -- I/O ----------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` atomically via tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_blocks(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").split(SEPARATOR)


def _write_blocks(path: Path, blocks: list[str]) -> None:
    """Normalize + persist blocks so re-writes are byte-stable (idempotency)."""
    clean = [b.strip() for b in blocks if b and b.strip()]
    text = SEPARATOR.join(clean) + (SEPARATOR if clean else "")
    _atomic_write_text(path, text)


# -- Lifecycle ----------------------------------------------------------------


def has_entry(log_path: str | Path, thesis_id: str) -> bool:
    """True if a pending OR resolved entry already exists for ``thesis_id``."""
    path = Path(log_path)
    for block in _read_blocks(path):
        stripped = block.strip()
        if stripped and _tag_thesis_id(stripped.splitlines()[0].strip()) == thesis_id:
            return True
    return False


def store_pending(
    log_path: str | Path,
    thesis_id: str,
    ticker: str,
    rating: str,
    decision: str,
) -> bool:
    """Append a *pending* entry. Idempotent: skip if ``thesis_id`` already logged.

    Returns True if written, False if skipped (an entry already exists).
    """
    path = Path(log_path)
    if has_entry(path, thesis_id):
        return False
    tag = f"[{thesis_id} | {ticker} | {rating} | pending]"
    entry = f"{tag}\n\nDECISION:\n{(decision or '(no decision recorded)').strip()}"
    _write_blocks(path, _read_blocks(path) + [entry])
    return True


def resolve(
    log_path: str | Path,
    thesis_id: str,
    *,
    raw_return: float | None,
    alpha: float | None,
    holding_days: int | None,
    reflection: str,
) -> bool:
    """Resolve the pending entry for ``thesis_id`` in place.

    Rewrites the tag with ``raw / alpha / holding-days`` and appends a
    REFLECTION section. Idempotent: if no *pending* entry matches (already
    resolved, or never stored) nothing is written and False is returned. Uses
    an atomic temp-file write so a crash mid-update never corrupts the log.
    """
    path = Path(log_path)
    blocks = _read_blocks(path)
    updated = False
    new_blocks: list[str] = []
    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        lines = stripped.splitlines()
        tag_line = lines[0].strip()
        if (
            not updated
            and _tag_thesis_id(tag_line) == thesis_id
            and tag_line.endswith("| pending]")
        ):
            fields = [f.strip() for f in tag_line[1:-1].split("|")]
            ticker = fields[1] if len(fields) > 1 else "?"
            rating = fields[2] if len(fields) > 2 else "?"
            holding = "n/a" if holding_days is None else f"{int(holding_days)}d"
            new_tag = (
                f"[{thesis_id} | {ticker} | {rating} | "
                f"{_fmt_pct(raw_return)} | {_fmt_pct(alpha)} | {holding}]"
            )
            rest = "\n".join(lines[1:]).strip()
            new_blocks.append(f"{new_tag}\n\n{rest}\n\nREFLECTION:\n{reflection.strip()}")
            updated = True
        else:
            new_blocks.append(stripped)

    if not updated:
        return False
    _write_blocks(path, new_blocks)
    return True


# -- Read path ----------------------------------------------------------------


def _parse_entry(raw: str) -> dict | None:
    stripped = raw.strip()
    if not stripped:
        return None
    lines = stripped.splitlines()
    tag_line = lines[0].strip()
    if not (tag_line.startswith("[") and tag_line.endswith("]")):
        return None
    fields = [f.strip() for f in tag_line[1:-1].split("|")]
    if len(fields) < 4:
        return None
    pending = fields[3] == "pending"
    entry = {
        "thesis_id": fields[0],
        "ticker": fields[1],
        "rating": fields[2],
        "pending": pending,
        "raw_return": None if pending else fields[3],
        "alpha": fields[4] if len(fields) > 4 else None,
        "holding": fields[5] if len(fields) > 5 else None,
    }
    body = "\n".join(lines[1:]).strip()
    d = _DECISION_RE.search(body)
    r = _REFLECTION_RE.search(body)
    entry["decision"] = d.group(1).strip() if d else ""
    entry["reflection"] = r.group(1).strip() if r else ""
    return entry


def load_entries(log_path: str | Path) -> list[dict]:
    """Parse all entries from the log, oldest first."""
    path = Path(log_path)
    entries = []
    for block in _read_blocks(path):
        parsed = _parse_entry(block)
        if parsed:
            entries.append(parsed)
    return entries


def _format_full(e: dict) -> str:
    raw = e["raw_return"] or "n/a"
    alpha = e["alpha"] or "n/a"
    holding = e["holding"] or "n/a"
    tag = f"[{e['ticker']} | {e['rating']} | raw {raw} | alpha {alpha} | {holding}]"
    parts = [tag, f"DECISION: {e['decision']}"]
    if e["reflection"]:
        parts.append(f"REFLECTION: {e['reflection']}")
    return "\n".join(parts)


def _format_reflection_only(e: dict) -> str:
    tag = f"[{e['ticker']} | alpha {e['alpha'] or 'n/a'}]"
    if e["reflection"]:
        return f"{tag} {e['reflection']}"
    text = e["decision"][:300]
    suffix = "..." if len(e["decision"]) > 300 else ""
    return f"{tag} {text}{suffix}"


def get_past_context(
    log_path: str | Path,
    ticker: str,
    n_same: int = 3,
    n_cross: int = 3,
) -> str:
    """Return a compact past-context block for prompt injection.

    Emits the ``n_same`` most-recent resolved entries for ``ticker`` in full
    (decision + reflection) plus the ``n_cross`` most-recent resolved entries
    for *other* tickers as reflection-only lessons. Pending entries are never
    surfaced. Returns "" when there is nothing to inject.
    """
    want = (ticker or "").upper()
    entries = [e for e in load_entries(log_path) if not e["pending"]]
    if not entries:
        return ""

    same, cross = [], []
    for e in reversed(entries):
        if len(same) >= n_same and len(cross) >= n_cross:
            break
        if e["ticker"].upper() == want and len(same) < n_same:
            same.append(e)
        elif e["ticker"].upper() != want and len(cross) < n_cross:
            cross.append(e)

    if not same and not cross:
        return ""

    parts = []
    if same:
        parts.append(f"Past theses on {want} (most recent first):")
        parts.extend(_format_full(e) for e in same)
    if cross:
        parts.append("Recent cross-ticker lessons:")
        parts.extend(_format_reflection_only(e) for e in cross)
    return "\n\n".join(parts)
