"""Shared path setup for live-analytics-dashboard tests."""

import os
import sys

# Add the scripts directory to sys.path so build_dashboard / csp_check import cleanly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
