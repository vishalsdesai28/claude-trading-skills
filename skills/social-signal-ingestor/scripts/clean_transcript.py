#!/usr/bin/env python3
"""Collapse a YouTube auto-caption VTT into plain prose text.

yt-dlp's auto-subs use a rolling 2-line display: each cue re-shows part of
the previous cue's text plus a few new words, and near-duplicate zero-duration
cues pin down final word timings. Read verbatim, a VTT is 3-4x the size of
the actual spoken words. This extracts just the new text from each cue and
joins it into normal prose, dropping timestamps and cue tags.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

TAG_RE = re.compile(r"<[^>]*>")
TIMING_LINE_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}")


def _cue_texts(vtt_text: str) -> list[str]:
    cues: list[str] = []
    for block in re.split(r"\n\s*\n", vtt_text):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        # Drop an optional cue identifier line preceding the timing line.
        if not TIMING_LINE_RE.match(lines[0]) and len(lines) > 1 and TIMING_LINE_RE.match(lines[1]):
            lines = lines[1:]
        if not TIMING_LINE_RE.match(lines[0]):
            continue  # header lines: WEBVTT, Kind:, Language:, etc.
        text_lines = lines[1:]
        text = " ".join(TAG_RE.sub("", line) for line in text_lines)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            cues.append(text)
    return cues


def clean_vtt_text(vtt_text: str) -> str:
    cues = _cue_texts(vtt_text)
    parts: list[str] = []
    prev = ""
    for cue in cues:
        if cue == prev or prev.endswith(cue):
            continue  # zero-duration anchor cue repeating already-seen tail
        if cue.startswith(prev):
            parts.append(cue[len(prev) :].strip())
        else:
            parts.append(cue)
        prev = cue
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def clean_vtt_file(vtt_path: Path) -> str:
    return clean_vtt_text(vtt_path.read_text(errors="replace"))


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: clean_transcript.py <subtitles.vtt> [out.clean.txt]", file=sys.stderr)
        return 1
    vtt_path = Path(sys.argv[1])
    cleaned = clean_vtt_file(vtt_path)
    default_out = vtt_path.parent / (vtt_path.name[: -len(vtt_path.suffix)] + ".clean.txt")
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else default_out
    out_path.write_text(cleaned)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
