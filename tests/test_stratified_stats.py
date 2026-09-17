"""Tests for the stratified (confounder-adjusted) statistics.

Reference values for the 2x2xK tests are R's own ``mantelhaen.test`` example (Agresti's
penicillin and rabbits data): Mantel-Haenszel X-squared = 3.9286 (p = 0.04747) and the
exact conditional test S = 16 (p = 0.03994). The stratified log-rank test is checked
through properties any correct implementation must satisfy.
"""

import math
import random

import pytest

from cbioportal_mcp import distribution_stats as ds
from cbioportal_mcp.cooccurrence_stats import (
    cmh_test,
    exact_conditional_test,
    fisher_exact_two_sided,
    mantel_fleiss_criterion,
    mantel_haenszel_odds_ratio,
    stratified_association_test,
)
from cbioportal_mcp.survival_stats import logrank_test, stratified_logrank_test

# Agresti (2002): rows Delay (none / 1.5h), columns Response (cured / died), five
# penicillin levels. [[a, b], [c, d]] per stratum.
RABBITS = [(0, 6, 0, 5), (3, 3, 0, 6), (6, 0, 2, 4), (5, 1, 6, 0), (2, 0, 5, 0)]


# --- 2x2xK -------------------------------------------------------------------


def test_cmh_matches_r_mantelhaen_test():
    r = cmh_test(RABBITS)
    assert r["chi_square"] == pytest.approx(3.928571, abs=1e-6)
    assert r["p_value"] == pytest.approx(0.04747, abs=5e-6)
    assert r["observed"] == 16 and r["expected"] == pytest.approx(13.0)


def test_exact_conditional_matches_r_mantelhaen_test_exact():
    r = exact_conditional_test(RABBITS)
    assert r["statistic"] == 16
    assert r["p_value"] == pytest.approx(0.03994, abs=5e-6)


def test_mantel_haenszel_odds_ratio_hand_computed():
    # sum(a*d/n) = 18/12 + 24/12 = 3.5; sum(b*c/n) = 6/12 = 0.5.
    assert mantel_haenszel_odds_ratio(RABBITS) == pytest.approx(7.0)
    assert mantel_haenszel_odds_ratio([(0, 0, 0, 5)]) is None
    assert mantel_haenszel_odds_ratio([(3, 0, 0, 3)]) == math.inf


def test_single_stratum_exact_test_is_fishers_exact_test():
    rng = random.Random(11)
    for _ in range(500):
        t = tuple(rng.randint(0, 25) for _ in range(4))
        assert exact_conditional_test([t])["p_value"] == pytest.approx(
            fisher_exact_two_sided(*t), rel=1e-9, abs=1e-300
        )


def test_stratum_order_does_not_matter():
    shuffled = list(reversed(RABBITS))
    assert exact_conditional_test(shuffled)["p_value"] == pytest.approx(
        exact_conditional_test(RABBITS)["p_value"]
    )
    assert cmh_test(shuffled)["chi_square"] == pytest.approx(cmh_test(RABBITS)["chi_square"])


def test_uninformative_strata_change_nothing():
    padded = RABBITS + [(0, 0, 0, 7), (4, 0, 0, 0), (1, 0, 0, 0)]
    assert exact_conditional_test(padded)["p_value"] == pytest.approx(
        exact_conditional_test(RABBITS)["p_value"]
    )
    assert cmh_test(padded)["chi_square"] == pytest.approx(cmh_test(RABBITS)["chi_square"])


def test_confounding_by_stratum_is_removed():
    """Simpson's-paradox shape: within each stratum the genes are independent, but one
    stratum has both genes common and the other has both rare, so the pooled table
    shows strong co-occurrence."""
    common = (40, 40, 40, 40)  # OR 1 within the stratum
    rare = (1, 19, 19, 361)  # OR 1 within the stratum
    pooled = tuple(x + y for x, y in zip(common, rare, strict=False))
    assert fisher_exact_two_sided(*pooled) < 1e-6
    adjusted = stratified_association_test([common, rare])
    assert adjusted["p_value"] > 0.5
    assert adjusted["mh_odds_ratio"] == pytest.approx(1.0)


def test_real_association_survives_stratification():
    strata = [(30, 10, 10, 30), (12, 4, 5, 20)]
    adjusted = stratified_association_test(strata)
    assert adjusted["p_value"] < 1e-4
    assert adjusted["log2_mh_odds_ratio"] > 2


def test_mantel_fleiss_routes_sparse_tables_to_the_exact_test():
    assert mantel_fleiss_criterion(RABBITS) == pytest.approx(4.0)
    sparse = stratified_association_test(RABBITS)
    assert sparse["method"] == "exact_conditional"
    assert sparse["p_value"] == pytest.approx(0.03994, abs=5e-6)
    assert sparse["n_strata"] == 5 and sparse["n_informative_strata"] == 3

    large = stratified_association_test([(300, 200, 200, 300), (150, 100, 90, 160)])
    assert large["method"] == "cmh_chi_square"
    assert large["chi_square"] > 0
    # Exact and approximate agree closely where the approximation is meant to be used.
    exact = exact_conditional_test([(30, 20, 20, 30), (15, 10, 9, 16)])["p_value"]
    approx = cmh_test([(30, 20, 20, 30), (15, 10, 9, 16)])["p_value"]
    assert exact == pytest.approx(approx, rel=0.25)


def test_no_information_gives_p_one_and_a_finite_odds_ratio():
    r = stratified_association_test([(0, 0, 0, 10), (0, 5, 0, 5)])
    assert r["method"] is None and r["p_value"] == 1.0
    assert math.isfinite(r["log2_mh_odds_ratio"])


def test_perfect_exclusivity_has_finite_log2_odds_ratio():
    r = stratified_association_test([(0, 10, 10, 0), (0, 8, 7, 0)])
    assert r["mh_odds_ratio"] == 0.0
    assert r["log2_mh_odds_ratio"] < -3
    assert r["p_value"] < 1e-6


def test_negative_counts_rejected():
    with pytest.raises(ValueError):
        cmh_test([(1, -1, 2, 3)])


# --- stratified log-rank ------------------------------------------------------


def _obs(rng, n, scale, event_rate=0.7):
    return [(rng.expovariate(1 / scale), 1 if rng.random() < event_rate else 0) for _ in range(n)]


def test_single_stratum_equals_the_unstratified_test():
    rng = random.Random(5)
    groups = {"a": _obs(rng, 40, 10), "b": _obs(rng, 35, 16)}
    plain = logrank_test(groups)
    strat = stratified_logrank_test({"only": groups})
    assert strat["chi_square"] == pytest.approx(plain["chi_square"])
    assert strat["p_value"] == pytest.approx(plain["p_value"])
    assert strat["test"] == "stratified log-rank"
    assert strat["n_strata"] == 1 and strat["n_informative_strata"] == 1


def test_duplicated_strata_double_the_statistic():
    rng = random.Random(6)
    groups = {"a": _obs(rng, 30, 8), "b": _obs(rng, 30, 14)}
    one = stratified_logrank_test({"s1": groups})
    two = stratified_logrank_test({"s1": groups, "s2": groups})
    assert two["chi_square"] == pytest.approx(2 * one["chi_square"])


def test_stratification_removes_a_between_stratum_prognosis_gap():
    """Group membership is confounded with a stratum of very different prognosis."""
    rng = random.Random(9)
    good = {"altered": _obs(rng, 200, 60), "wild-type": _obs(rng, 40, 60)}
    bad = {"altered": _obs(rng, 40, 5), "wild-type": _obs(rng, 200, 5)}
    pooled = {
        "altered": good["altered"] + bad["altered"],
        "wild-type": good["wild-type"] + bad["wild-type"],
    }
    assert logrank_test(pooled)["p_value"] < 1e-6
    assert stratified_logrank_test({"good": good, "bad": bad})["p_value"] > 0.01


def test_three_groups_and_groups_missing_from_a_stratum():
    rng = random.Random(12)
    strata = {
        "x": {"a": _obs(rng, 20, 5), "b": _obs(rng, 20, 9), "c": _obs(rng, 20, 20)},
        "y": {"a": _obs(rng, 15, 5), "c": _obs(rng, 15, 20)},  # no "b" here
        "z": {"b": _obs(rng, 10, 9)},  # one group only: uninformative
    }
    r = stratified_logrank_test(strata)
    assert r["df"] == 2 and r["p_value"] is not None
    assert r["n_strata"] == 3 and r["n_informative_strata"] == 2
    assert [g["group"] for g in r["group_observed_expected"]] == ["a", "b", "c"]


def test_observed_minus_expected_balances_even_when_the_last_patient_dies_alone():
    # Group "a" has the longest follow-up and dies with nobody else at risk (n == 1).
    groups = {"a": [(2, 1), (9, 1)], "b": [(1, 1), (3, 1), (4, 0)]}
    lr = logrank_test(groups)
    total = sum(g["observed"] - g["expected"] for g in lr["group_observed_expected"])
    assert total == pytest.approx(0.0, abs=1e-12)
    oe = {g["group"]: g for g in lr["group_observed_expected"]}
    # t=1: n=5 (a:2) -> E_a += 2/5; t=2: n=4 (a:2) -> 2/4; t=3: n=3 (a:1) -> 1/3;
    # t=9: n=1 (only a) -> E_a += 1, the term the old loop skipped.
    assert oe["a"]["expected"] == pytest.approx(0.4 + 0.5 + 1 / 3 + 1.0)


def test_stratified_two_group_statistic_does_not_depend_on_group_order():
    rng = random.Random(21)
    strata = {
        f"s{i}": {"x": _obs(rng, rng.randint(1, 6), 10), "y": _obs(rng, rng.randint(1, 6), 14)}
        for i in range(12)
    }
    one = stratified_logrank_test(strata, order=["x", "y"])
    two = stratified_logrank_test(strata, order=["y", "x"])
    assert one["chi_square"] == pytest.approx(two["chi_square"])
    assert sum(g["observed"] - g["expected"] for g in one["group_observed_expected"]) == (
        pytest.approx(0.0, abs=1e-9)
    )


def test_stratified_logrank_needs_two_groups():
    r = stratified_logrank_test({"s": {"a": [(1, 1)]}})
    assert r["p_value"] is None and "reason" in r


def test_stratified_logrank_with_no_informative_stratum():
    r = stratified_logrank_test({"s1": {"a": [(1, 1), (2, 1)]}, "s2": {"b": [(3, 1)]}})
    assert r["p_value"] is None
    assert "No stratum contains two groups" in r["reason"]


# --- distribution stats -------------------------------------------------------


def test_quantile_matches_r_type_7():
    xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert ds.quantile(xs, 0.25) == pytest.approx(3.25)
    assert ds.quantile(xs, 0.5) == pytest.approx(5.5)
    assert ds.quantile(xs, 0.9) == pytest.approx(9.1)
    assert ds.quantile([4.0], 0.3) == 4.0
    with pytest.raises(ValueError):
        ds.quantile([], 0.5)


def test_describe():
    d = ds.describe([2, 4, 4, 4, 5, 5, 7, 9, None, "x", float("nan")])
    assert d["n"] == 8
    assert d["mean"] == pytest.approx(5.0)
    assert d["sd"] == pytest.approx(2.13809, abs=1e-5)
    assert d["median"] == pytest.approx(4.5)
    assert (d["min"], d["max"]) == (2.0, 9.0)
    assert ds.describe([])["mean"] is None


def test_histogram_bins_are_half_open_with_a_closed_last_bin():
    h = ds.histogram([0.0, 0.1, 0.25, 0.5, 0.99, 1.0, 1.2], bins=4, value_range=(0.0, 1.0))
    assert h["edges"] == pytest.approx([0.0, 0.25, 0.5, 0.75, 1.0])
    assert h["counts"] == [2, 1, 1, 2]  # 0.25 opens bin 2; 1.0 closes the last bin
    assert h["n_binned"] == 6 and h["n_outside"] == 1


def test_histogram_automatic_bins_and_constant_values():
    rng = random.Random(1)
    h = ds.histogram([rng.gauss(0, 1) for _ in range(1000)])
    assert ds.DEFAULT_MIN_BINS <= len(h["counts"]) <= ds.DEFAULT_MAX_BINS
    assert h["n_binned"] == 1000
    flat = ds.histogram([3.0, 3.0, 3.0])
    assert flat["n_binned"] == 3
    with pytest.raises(ValueError):
        ds.histogram([])
    with pytest.raises(ValueError):
        ds.histogram([1.0], bins=0)


def test_quantile_split_quartiles_and_ties():
    values = {f"p{i}": float(i) for i in range(1, 13)}
    q = ds.quantile_split(values, "quartiles")
    assert q["cutoffs"] == pytest.approx({"q1": 3.75, "q2": 6.5, "q3": 9.25})
    assert [len(g["ids"]) for g in q["groups"]] == [3, 3, 3, 3]

    tb = ds.quantile_split(values, "top_vs_bottom_quartile")
    assert [g["name"] for g in tb["groups"]] == ["top quartile", "bottom quartile"]
    assert tb["groups"][0]["ids"] == ["p10", "p11", "p12"]
    assert tb["n_excluded"] == 6

    rest = ds.quantile_split(values, "top_quartile_vs_rest")
    assert len(rest["groups"][1]["ids"]) == 9

    tied = {f"p{i}": 1.0 for i in range(8)} | {"hi": 5.0}
    med = ds.quantile_split(tied, "median")
    assert len(med["groups"][0]["ids"]) == 8  # ties fall into the lower group


def test_quantile_split_validation():
    with pytest.raises(ValueError, match="split must be one of"):
        ds.quantile_split({"a": 1, "b": 2, "c": 3, "d": 4}, "deciles")
    with pytest.raises(ValueError, match="at least four"):
        ds.quantile_split({"a": 1, "b": 2}, "median")
