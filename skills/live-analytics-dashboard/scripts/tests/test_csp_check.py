"""Tests for the CSP-safety gate (csp_check.py)."""

from pathlib import Path

import csp_check
import pytest
from csp_check import (
    CspViolationError,
    assert_csp_safe,
    scan_file,
    scan_text,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOOD = FIXTURES / "good_template.html"
BAD = FIXTURES / "bad_template.html"


class TestGoodTemplate:
    def test_good_template_has_no_violations(self):
        assert scan_file(GOOD) == []

    def test_assert_csp_safe_passes_on_good(self):
        assert assert_csp_safe([GOOD]) is True

    def test_dom_property_assignment_is_allowed(self):
        # `el.onfocus = handleClick` is CSP-safe (property, not attribute).
        assert scan_text("el.onfocus = handleClick;") == []

    def test_add_event_listener_is_allowed(self):
        assert scan_text("btn.addEventListener('click', onHeaderClick);") == []

    def test_function_reference_timer_is_allowed(self):
        assert scan_text("setInterval(refresh, 5000);") == []


class TestBadTemplate:
    def test_bad_template_has_violations(self):
        violations = scan_file(BAD)
        assert violations, "expected the bad template to trip the gate"

    def test_bad_template_categories(self):
        cats = {v.category for v in scan_file(BAD)}
        assert {"inline-handler", "eval", "new-function", "javascript-url", "string-timer"} <= cats

    def test_assert_csp_safe_raises_on_bad(self):
        with pytest.raises(CspViolationError):
            assert_csp_safe([BAD])

    def test_error_carries_violation_list(self):
        try:
            assert_csp_safe([BAD])
        except CspViolationError as exc:
            assert len(exc.violations) >= 5
        else:
            pytest.fail("expected CspViolationError")


class TestPatterns:
    def test_inline_onclick_attribute_flagged(self):
        v = scan_text('<button onclick="go()">x</button>')
        assert any(x.category == "inline-handler" for x in v)

    def test_inline_onmouseover_attribute_flagged(self):
        v = scan_text("<div onmouseover='hover()'></div>")
        assert any(x.category == "inline-handler" for x in v)

    def test_string_timeout_flagged(self):
        assert any(x.category == "string-timer" for x in scan_text('setTimeout("tick()", 5)'))

    def test_javascript_url_flagged(self):
        assert any(
            x.category == "javascript-url" for x in scan_text('<a href="javascript:void(0)">x</a>')
        )

    def test_identifier_containing_on_not_flagged(self):
        # `iconClick` and `content=` must not be treated as handlers.
        assert scan_text('var iconClick = 1; <meta content="width">') == []

    def test_line_numbers_reported(self):
        text = 'line one\n<button onclick="x">\nline three'
        v = scan_text(text)
        assert v[0].line == 2


class TestCli:
    def test_main_returns_1_on_violations(self):
        assert csp_check.main([str(BAD)]) == 1

    def test_main_returns_0_on_clean(self):
        assert csp_check.main([str(GOOD)]) == 0

    def test_main_scans_directory(self):
        # Scanning the fixtures dir must find the bad template's violations.
        assert csp_check.main([str(FIXTURES)]) == 1
