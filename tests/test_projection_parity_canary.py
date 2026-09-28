"""Unit tests for the over-count canary in tests/test_projection_parity_live.py.

The canary is what proves the live parity test can catch the bug, so it must
only pass on a real over-count. These run without ClickHouse.
"""

import pytest
from test_projection_parity_live import check_overcount_canary

BASELINE = "20000\n"


def test_real_overcount_passes():
    check_overcount_canary("canary", BASELINE, "36384\n", BASELINE)


@pytest.mark.parametrize(
    "on_default",
    ["ERROR 60", "ERROR ?", "", "\n", "20000\n1\n", "20000,1", "-5", "abc"],
    ids=["error", "error-unknown", "empty", "blank", "two-rows", "two-cols", "negative", "text"],
)
def test_non_count_default_result_fails(on_default):
    with pytest.raises(AssertionError, match="is not a single count"):
        check_overcount_canary("canary", BASELINE, on_default, BASELINE)


def test_undercount_fails():
    with pytest.raises(AssertionError, match="no over-count"):
        check_overcount_canary("canary", BASELINE, "19999\n", BASELINE)


def test_equal_counts_fail():
    with pytest.raises(AssertionError, match="no over-count"):
        check_overcount_canary("canary", BASELINE, BASELINE, BASELINE)


@pytest.mark.parametrize("off", ["ERROR 60", ""], ids=["error", "empty"])
def test_non_count_baseline_fails(off):
    with pytest.raises(AssertionError, match="off is not a single count"):
        check_overcount_canary("canary", off, "36384\n", "36384\n")


@pytest.mark.parametrize("on_safe", ["ERROR 164", ""], ids=["error", "empty"])
def test_non_count_safe_result_fails(on_safe):
    with pytest.raises(AssertionError, match="on_safe is not a single count"):
        check_overcount_canary("canary", BASELINE, "36384\n", on_safe)


def test_safe_result_must_match_baseline():
    with pytest.raises(AssertionError, match="PROJECTION_SAFE_SETTINGS gives 36384"):
        check_overcount_canary("canary", BASELINE, "36384\n", "36384\n")
