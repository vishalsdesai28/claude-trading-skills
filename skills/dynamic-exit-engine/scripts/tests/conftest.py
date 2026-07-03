"""Shared fixtures for Dynamic Exit Engine tests."""

import os
import sys

# Add scripts directory to path so manage_exit can be imported.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Add tests directory to path so helpers can be imported.
sys.path.insert(0, os.path.dirname(__file__))
