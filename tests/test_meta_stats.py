"""Tests for ``meta_stats``: Wilson intervals, DerSimonian-Laird pooling and the
homogeneity test behind the cross-study alteration-frequency tool.

Reference values come from three independent places: textbook Wilson intervals
(0/10 -> [0, 0.278], 5/10 -> [0.237, 0.763]); the two-study TP53 lung
adenocarcinoma example worked in ``docs/cross-study-meta-analysis-plan.md``
(TCGA 295/566 vs MSK-CHORD 2695/5957); and, for k = 2, closed forms that do not
share code with the summations under test -- Q = (y1 - y2)^2 / (v1 + v2),
C = 2 w1 w2 / (w1 + w2), and the 2x2 chi-square N(ad - bc)^2 / (row x column
products).
"""

import math

import pytest

from cbioportal_mcp.cooccurrence_stats import fisher_exact_two_sided
from cbioportal_mcp.meta_stats import (
    expit,
    heterogeneity,
    homogeneity_test,
    logit,
    pooled_proportion,
    wilson_interval,
)

TCGA = (295, 566)
CHORD = (2695, 5957)


# --- Wilson interval ---------------------------------------------------------


def test_wilson_textbook_values():
    lo, hi = wilson_interval(5, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.2366, 0.7634)
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0 and round(hi, 4) == 0.2775
    lo, hi = wilson_interval(10, 10)
    assert round(lo, 4) == 0.7225 and hi == 1.0


def test_wilson_example_rows():
    lo, hi = wilson_interval(*TCGA)
    assert (round(100 * lo, 1), round(100 * hi, 1)) == (48.0, 56.2)
    lo, hi = wilson_interval(*CHORD)
    assert (round(100 * lo, 1), round(100 * hi, 1)) == (44.0, 46.5)


@pytest.mark.parametrize("a, n", [(0, 1), (1, 1), (3, 7), (50, 100), (999, 1000)])
def test_wilson_contains_the_point_estimate(a, n):
    lo, hi = wilson_interval(a, n)
    assert 0.0 <= lo <= a / n <= hi <= 1.0


def test_wilson_wider_at_higher_confidence():
    lo95, hi95 = wilson_interval(30, 100, 0.95)
    lo99, hi99 = wilson_interval(30, 100, 0.99)
    assert lo99 < lo95 and hi99 > hi95


@pytest.mark.parametrize("a, n", [(1, 0), (5, 4), (-1, 10)])
def test_wilson_rejects_bad_counts(a, n):
    with pytest.raises(ValueError):
        wilson_interval(a, n)


def test_wilson_rejects_bad_conf_level():
    with pytest.raises(ValueError):
        wilson_interval(1, 10, conf_level=1.5)


# --- logit / expit -----------------------------------------------------------


def test_logit_expit_roundtrip():
    for p in (0.01, 0.3, 0.5, 0.9):
        assert math.isclose(expit(logit(p)), p, rel_tol=1e-12)


def test_expit_does_not_overflow():
    assert expit(800.0) == 1.0
    assert expit(-800.0) == 0.0


def test_logit_rejects_boundaries():
    with pytest.raises(ValueError):
        logit(0.0)
    with pytest.raises(ValueError):
        logit(1.0)


# --- pooled_proportion (DerSimonian-Laird) ------------------------------------


def test_two_study_example_matches_the_plan():
    res = pooled_proportion([TCGA, CHORD])
    assert res["k"] == 2 and res["df"] == 1
    fe, re_ = res["fixed"], res["random"]
    assert round(100 * fe["proportion"], 1) == 45.8
    assert [round(100 * x, 1) for x in fe["ci"]] == [44.6, 47.1]
    assert round(100 * re_["proportion"], 1) == 48.4
    assert [round(100 * x, 1) for x in re_["ci"]] == [41.7, 55.1]
    assert res["q"] == pytest.approx(9.805, abs=1e-3)
    assert res["tau2"] == pytest.approx(0.0342, abs=5e-4)
    assert res["i2"] == pytest.approx(0.898, abs=1e-3)
    assert res["p_heterogeneity"] == pytest.approx(0.00174, rel=1e-2)
    assert [round(w, 3) for w in re_["weights"]] == [0.458, 0.542]
    assert sum(fe["weights"]) == pytest.approx(1.0) and sum(re_["weights"]) == pytest.approx(1.0)


def test_two_study_closed_forms():
    """Q and C have closed forms for k = 2 that bypass the summation code."""
    res = pooled_proportion([TCGA, CHORD])
    y, v = [], []
    for a, n in (TCGA, CHORD):
        y.append(math.log(a / (n - a)))
        v.append(1 / a + 1 / (n - a))
    q_closed = (y[0] - y[1]) ** 2 / (v[0] + v[1])
    w = [1 / x for x in v]
    c_closed = 2 * w[0] * w[1] / (w[0] + w[1])
    tau2_closed = max(0.0, (q_closed - 1) / c_closed)
    assert res["q"] == pytest.approx(q_closed, rel=1e-12)
    assert res["tau2"] == pytest.approx(tau2_closed, rel=1e-12)
    # Random-effects weights are 1 / (v_i + tau2), normalised.
    w_re = [1 / (x + tau2_closed) for x in v]
    assert res["random"]["weights"][0] == pytest.approx(w_re[0] / sum(w_re), rel=1e-12)


def test_identical_studies_have_no_heterogeneity():
    res = pooled_proportion([(50, 100)] * 3)
    assert res["q"] == pytest.approx(0.0, abs=1e-12)
    assert res["tau2"] == 0.0 and res["i2"] == 0.0 and res["p_heterogeneity"] == 1.0
    assert res["random"]["proportion"] == pytest.approx(res["fixed"]["proportion"])
    assert res["random"]["proportion"] == pytest.approx(0.5)
    assert res["random"]["weights"] == pytest.approx([1 / 3] * 3)


def test_tau2_is_floored_at_zero_when_q_below_df():
    res = pooled_proportion([(50, 100), (51, 100), (49, 100)])
    assert res["q"] < res["df"]
    assert res["tau2"] == 0.0 and res["i2"] == 0.0
    assert res["random"]["proportion"] == pytest.approx(res["fixed"]["proportion"])
    assert res["random"]["ci"] == pytest.approx(res["fixed"]["ci"])


def test_continuity_correction_keeps_boundary_studies_finite():
    res = pooled_proportion([(0, 20), (5, 20)])
    assert math.isfinite(res["random"]["proportion"]) and math.isfinite(res["q"])
    assert 0.0 < res["random"]["proportion"] < 5 / 20
    res = pooled_proportion([(20, 20), (19, 20)])
    assert 19 / 20 < res["random"]["proportion"] < 1.0


def test_pooling_requires_two_valid_studies():
    with pytest.raises(ValueError):
        pooled_proportion([(1, 2)])
    with pytest.raises(ValueError):
        pooled_proportion([])
    with pytest.raises(ValueError):
        pooled_proportion([(0, 0), (1, 2)])


def test_pooled_interval_widens_at_higher_confidence():
    lo95, hi95 = pooled_proportion([TCGA, CHORD], 0.95)["random"]["ci"]
    lo99, hi99 = pooled_proportion([TCGA, CHORD], 0.99)["random"]["ci"]
    assert lo99 < lo95 and hi99 > hi95


# --- heterogeneity -----------------------------------------------------------


def test_heterogeneity_edges():
    assert heterogeneity(0.0, 1) == (0.0, 1.0)
    i2, p = heterogeneity(3.0, 5)
    assert i2 == 0.0 and 0.0 < p < 1.0
    i2, _ = heterogeneity(20.0, 4)
    assert i2 == pytest.approx(0.8)
    with pytest.raises(ValueError):
        heterogeneity(1.0, 0)
    with pytest.raises(ValueError):
        heterogeneity(-1.0, 1)


# --- homogeneity_test --------------------------------------------------------


def test_homogeneity_chi_square_example():
    res = homogeneity_test([TCGA, CHORD])
    assert res["test"] == "chi_square_homogeneity" and res["df"] == 1 and res["k"] == 2
    assert res["statistic"] == pytest.approx(9.853, abs=2e-3)
    assert res["p_value"] == pytest.approx(0.0017, abs=2e-4)
    # Independent closed form for the 2x2 table.
    a, b = TCGA[0], TCGA[1] - TCGA[0]
    c, d = CHORD[0], CHORD[1] - CHORD[0]
    n = a + b + c + d
    closed = n * (a * d - b * c) ** 2 / ((a + b) * (c + d) * (a + c) * (b + d))
    assert res["statistic"] == pytest.approx(closed, rel=1e-12)
    assert res["min_expected_cell"] == pytest.approx(566 * (2990 / 6523))


def test_homogeneity_uses_fisher_for_two_small_studies():
    res = homogeneity_test([(1, 8), (6, 9)])
    assert res["test"] == "fisher_exact"
    assert res["statistic"] is None and res["df"] is None
    assert res["p_value"] == pytest.approx(fisher_exact_two_sided(1, 7, 6, 3))
    assert res["min_expected_cell"] < 5


def test_homogeneity_three_small_studies_stay_chi_square_but_flag_cells():
    res = homogeneity_test([(1, 8), (6, 9), (2, 7)])
    assert res["test"] == "chi_square_homogeneity" and res["df"] == 2
    assert res["min_expected_cell"] < 5
    assert 0.0 < res["p_value"] <= 1.0


def test_homogeneity_degenerate_all_zero_or_all_altered():
    for studies in ([(0, 10), (0, 20)], [(10, 10), (20, 20)]):
        res = homogeneity_test(studies)
        assert res["statistic"] == 0.0 and res["p_value"] == 1.0


def test_homogeneity_requires_two_studies():
    with pytest.raises(ValueError):
        homogeneity_test([(3, 10)])
