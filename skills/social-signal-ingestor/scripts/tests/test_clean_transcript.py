"""Tests for clean_transcript.py — the VTT rolling-caption deduplicator."""

from clean_transcript import clean_vtt_text


def test_drops_exact_dup_and_extends_prefix():
    vtt = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000
buy NVDA now

00:00:02.000 --> 00:00:04.000
buy NVDA now

00:00:04.000 --> 00:00:06.000
buy NVDA now and AMD later
"""
    assert clean_vtt_text(vtt) == "buy NVDA now and AMD later"


def test_strips_tags_and_unescapes_html():
    vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
<c>AT&amp;T</c> rallies
"""
    assert clean_vtt_text(vtt) == "AT&T rallies"


def test_header_only_returns_empty():
    assert clean_vtt_text("WEBVTT\nKind: captions\n") == ""
