"""CSP-safety gate (grep-style scanner) for dashboard templates.

Fails a build when Content-Security-Policy-unsafe patterns are present in
served HTML/JS: inline ``on*`` event-handler attributes, ``eval()`` /
``new Function()``, ``javascript:`` URLs, and string-form
``setTimeout`` / ``setInterval``. These patterns are silently blocked by the
CSP that the claude.ai Artifact sandbox and most embedded-iframe hosts enforce
-- no error banner, just dead UI. Catch them before serving.

Import-safe with the stdlib only. Used two ways:
  * as a pre-serve build gate  -> ``assert_csp_safe([index_html])``
  * as a standalone CLI        -> ``python3 csp_check.py <path> [...]``

The scanner is deliberately strict: the CSP-safe alternative is always
``addEventListener`` + named function declarations (see references/csp_safety.md).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXTS = (".html", ".htm", ".js")

# Inline HTML event-handler attribute, e.g. `<button onclick="fn()">`.
# The negative lookbehind excludes CSP-*safe* DOM-property assignment
# (`el.onclick = fn`) and identifiers that merely contain "on" (`iconClick`).
# Requiring a quote after `=` further separates attribute handlers from
# property assignments to a function reference.
INLINE_HANDLER_RE = re.compile(r"""(?<![\w.])on[a-z]+\s*=\s*["']""")
EVAL_RE = re.compile(r"\beval\s*\(")
NEW_FUNCTION_RE = re.compile(r"\bnew\s+Function\s*\(")
JS_URL_RE = re.compile(r"""["']\s*javascript:""", re.IGNORECASE)
STRING_TIMER_RE = re.compile(r"""\bset(?:Timeout|Interval)\s*\(\s*["']""")

_CHECKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("inline-handler", INLINE_HANDLER_RE),
    ("eval", EVAL_RE),
    ("new-function", NEW_FUNCTION_RE),
    ("javascript-url", JS_URL_RE),
    ("string-timer", STRING_TIMER_RE),
)


@dataclass(frozen=True)
class Violation:
    """A single CSP-unsafe pattern match."""

    path: str
    line: int
    category: str
    text: str


class CspViolationError(RuntimeError):
    """Raised by ``assert_csp_safe`` when any violation is found."""

    def __init__(self, violations: list[Violation]) -> None:
        self.violations = list(violations)
        super().__init__(format_violations(self.violations))


def scan_text(text: str, source: str = "<string>") -> list[Violation]:
    """Return every CSP violation found in *text* (one per pattern per line)."""
    out: list[Violation] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for category, rx in _CHECKS:
            if rx.search(line):
                out.append(Violation(source, lineno, category, line.strip()[:200]))
    return out


def scan_file(path: str | Path) -> list[Violation]:
    """Scan a single file for CSP violations."""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    return scan_text(text, str(p))


def scan_paths(paths: list[str | Path], exts: tuple[str, ...] = DEFAULT_EXTS) -> list[Violation]:
    """Scan files and/or directories. Directories are walked for *exts* files."""
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in exts))
        else:
            files.append(p)
    out: list[Violation] = []
    for f in files:
        out.extend(scan_file(f))
    return out


def assert_csp_safe(paths: list[str | Path], exts: tuple[str, ...] = DEFAULT_EXTS) -> bool:
    """Raise ``CspViolationError`` if any scanned file has a violation."""
    violations = scan_paths(paths, exts=exts)
    if violations:
        raise CspViolationError(violations)
    return True


def format_violations(violations: list[Violation]) -> str:
    """Human-readable rendering of a violation list."""
    if not violations:
        return "No CSP violations found."
    lines = ["CSP-unsafe patterns found (see references/csp_safety.md):"]
    for v in violations:
        lines.append(f"  {v.path}:{v.line} [{v.category}] {v.text}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CSP-safety gate for dashboard templates.")
    ap.add_argument("paths", nargs="+", help="HTML/JS files or directories to scan")
    ap.add_argument(
        "--ext",
        action="append",
        default=None,
        help="Extra file extension to include when scanning directories (repeatable)",
    )
    args = ap.parse_args(argv)
    if args.ext:
        extra = tuple("." + e.lstrip(".") for e in args.ext)
        exts = tuple(dict.fromkeys(DEFAULT_EXTS + extra))
    else:
        exts = DEFAULT_EXTS
    violations = scan_paths(args.paths, exts=exts)
    stream = sys.stderr if violations else sys.stdout
    print(format_violations(violations), file=stream)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
