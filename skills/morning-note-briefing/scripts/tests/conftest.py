"""Pytest configuration for morning-note-briefing tests."""

import sys
from pathlib import Path

# Add the scripts directory to sys.path so tests import the module directly.
scripts_dir = Path(__file__).resolve().parents[1]
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
