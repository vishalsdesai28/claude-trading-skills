"""Shared fixtures for Intrinsic Value DCF tests."""

import json
import os
import sys
from pathlib import Path

# Add scripts directory to path so value_company can be imported.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Add tests directory to path for helpers.
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
ACME_FIXTURE = FIXTURE_DIR / "acme_fmp.json"


@pytest.fixture
def acme_raw() -> dict:
    """Raw FMP-shaped JSON for the fictional ACME company."""
    with open(ACME_FIXTURE, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def acme_financials(acme_raw):
    """Normalized CompanyFinancials for ACME."""
    from value_company import normalize_fmp

    return normalize_fmp(acme_raw, "ACME")
