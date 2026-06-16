"""Tests for the pure-Python co-occurrence statistics module.

These exercise the Fisher exact test, the log2 odds ratio, and the
Benjamini-Hochberg correction against hand-computed / well-known values, with no
cBioPortal or database knowledge.
"""

import math

import pytest

from cbioportal_mcp.cooccurrence_stats import (
    benjamini_hochberg,
    fisher_exact_two_sided,
    log2_odds_ratio,
)

# --- fisher_exact_two_sided --------------------------------------------------


def test_fisher_lady_tasting_tea():
    # Fisher's classic 2x2 [[3,1],[1,3]] has a known two-sided p of 34/70.
    assert fisher_exact_two_sided(3, 1, 1, 3) == pytest.approx(34 / 70)


def test_fisher_perfect_cooccurrence_is_tiny():
    # [[10,0],[0,10]]: the only tables as extreme are x=10 and x=0, each
    # 1/C(20,10); two-sided p = 2/184756.
    assert fisher_exact_two_sided(10, 0, 0, 10) == pytest.approx(2 / 184756)


def test_fisher_perfect_exclusivity_is_tiny():
    # The mirror table is equally extreme.
    assert fisher_exact_two_sided(0, 10, 10, 0) == pytest.approx(2 / 184756)


def test_fisher_independent_table_is_one():
    # A perfectly balanced table shows no association.
    assert fisher_exact_two_sided(5, 5, 5, 5) == pytest.approx(1.0)


def test_fisher_symmetry_under_row_and_col_swaps():
    p = fisher_exact_two_sided(8, 2, 1, 5)
    assert fisher_exact_two_sided(2, 8, 5, 1) == pytest.approx(p)  # swap columns
    assert fisher_exact_two_sided(1, 5, 8, 2) == pytest.approx(p)  # swap rows


@pytest.mark.parametrize(
    "table",
    [(0, 0, 0, 0), (0, 0, 5, 5), (5, 0, 5, 0), (0, 5, 0, 5)],
)
def test_fisher_degenerate_margins_return_one(table):
    assert fisher_exact_two_sided(*table) == 1.0


def test_fisher_rejects_negative_counts():
    with pytest.raises(ValueError):
        fisher_exact_two_sided(-1, 2, 3, 4)


def test_fisher_p_in_unit_interval():
    for tbl in [(7, 3, 2, 18), (1, 0, 0, 30), (12, 4, 9, 6), (2, 20, 18, 3)]:
        p = fisher_exact_two_sided(*tbl)
        assert 0.0 <= p <= 1.0


# --- log2_odds_ratio ---------------------------------------------------------


def test_log2_odds_ratio_sign_cooccurrence_vs_exclusivity():
    assert log2_odds_ratio(10, 0, 0, 10) > 0  # co-occurrence
    assert log2_odds_ratio(0, 10, 10, 0) < 0  # mutual exclusivity


def test_log2_odds_ratio_balanced_is_zero():
    assert log2_odds_ratio(5, 5, 5, 5) == pytest.approx(0.0)


def test_log2_odds_ratio_haldane_keeps_it_finite():
    # Zeros would make the raw OR 0 or inf; the +0.5 correction keeps it finite.
    val = log2_odds_ratio(10, 0, 0, 10)
    assert math.isfinite(val)
    assert val == pytest.approx(math.log2((10.5 * 10.5) / (0.5 * 0.5)))


# --- benjamini_hochberg ------------------------------------------------------


def test_bh_empty():
    assert benjamini_hochberg([]) == []


def test_bh_uniformly_scaled_example():
    # p = k*0.01 for k=1..5 all map to q=0.05.
    q = benjamini_hochberg([0.01, 0.02, 0.03, 0.04, 0.05])
    assert q == pytest.approx([0.05, 0.05, 0.05, 0.05, 0.05])


def test_bh_preserves_input_order_and_monotonicity():
    q = benjamini_hochberg([0.5, 0.001])
    assert q[1] == pytest.approx(0.002)  # 0.001 * 2 / 1
    assert q[0] == pytest.approx(0.5)  # 0.5 * 2 / 2
    # The smaller p-value gets the smaller q-value.
    assert q[1] < q[0]


def test_bh_clamped_to_one():
    q = benjamini_hochberg([0.8, 0.9, 0.95])
    assert all(v <= 1.0 for v in q)
