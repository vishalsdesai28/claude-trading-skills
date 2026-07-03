"""Shared fixtures/path setup for adversarial-trade-debate tests."""

import os
import sys

# Make the scripts directory importable so tests can import debate_kit.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
