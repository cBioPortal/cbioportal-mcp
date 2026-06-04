"""Tests for the pure-Python survival statistics (Kaplan-Meier + log-rank)."""

import math

from cbioportal_mcp.survival_stats import chi_square_sf, kaplan_meier, logrank_test

# Classic Freireich 6-MP vs placebo dataset (a standard log-rank reference).
FREIREICH_6MP = [
    (6, 1),
    (6, 1),
    (6, 1),
    (7, 1),
    (10, 1),
    (13, 1),
    (16, 1),
    (22, 1),
    (23, 1),
    (6, 0),
    (9, 0),
    (10, 0),
    (11, 0),
    (17, 0),
    (19, 0),
    (20, 0),
    (25, 0),
    (32, 0),
    (32, 0),
    (34, 0),
    (35, 0),
]
FREIREICH_PLACEBO = [
    (1, 1),
    (1, 1),
    (2, 1),
    (2, 1),
    (3, 1),
    (4, 1),
    (4, 1),
    (5, 1),
    (5, 1),
    (8, 1),
    (8, 1),
    (8, 1),
    (8, 1),
    (11, 1),
    (11, 1),
    (12, 1),
    (12, 1),
    (15, 1),
    (17, 1),
    (22, 1),
    (23, 1),
]


# --- chi_square_sf -----------------------------------------------------------


def test_chi_square_sf_critical_values():
    # Upper-tail critical values from chi-square tables.
    assert math.isclose(chi_square_sf(3.841, 1), 0.05, abs_tol=1e-3)
    assert math.isclose(chi_square_sf(6.635, 1), 0.01, abs_tol=1e-3)
    assert math.isclose(chi_square_sf(5.991, 2), 0.05, abs_tol=1e-3)
    assert math.isclose(chi_square_sf(7.815, 3), 0.05, abs_tol=1e-3)


def test_chi_square_sf_edges():
    assert chi_square_sf(0.0, 1) == 1.0
    assert chi_square_sf(-5.0, 1) == 1.0
    assert chi_square_sf(1000.0, 1) < 1e-9


# --- kaplan_meier ------------------------------------------------------------


def test_kaplan_meier_hand_computed():
    obs = [(1, 1), (2, 0), (3, 1), (4, 1)]
    km = kaplan_meier(obs)
    assert km["n_patients"] == 4
    assert km["n_events"] == 3
    assert km["n_censored"] == 1
    assert km["max_time"] == 4
    # First curve point is always (0, 1.0).
    assert km["curve"][0] == {
        "time": 0.0,
        "survival": 1.0,
        "at_risk": 4,
        "events": 0,
        "censored": 0,
    }
    # Survival drops to 0.75 at t=1, stays through the t=2 censor, 0.375 at t=3.
    by_time = {p["time"]: p for p in km["curve"]}
    assert math.isclose(by_time[1]["survival"], 0.75)
    assert math.isclose(by_time[2]["survival"], 0.75)
    assert by_time[2]["censored"] == 1
    assert math.isclose(by_time[3]["survival"], 0.375)
    # Median = first event time with survival <= 0.5.
    assert km["median_survival"] == 3


def test_kaplan_meier_freireich_medians():
    assert kaplan_meier(FREIREICH_6MP)["median_survival"] == 23
    assert kaplan_meier(FREIREICH_PLACEBO)["median_survival"] == 8


def test_kaplan_meier_all_censored_has_no_median():
    km = kaplan_meier([(5, 0), (10, 0), (15, 0)])
    assert km["n_events"] == 0
    assert km["median_survival"] is None
    # Survival never drops below 1.0.
    assert all(p["survival"] == 1.0 for p in km["curve"])


def test_kaplan_meier_empty():
    km = kaplan_meier([])
    assert km["n_patients"] == 0
    assert km["median_survival"] is None
    assert km["curve"] == [{"time": 0.0, "survival": 1.0, "at_risk": 0, "events": 0, "censored": 0}]


def test_kaplan_meier_at_risk_at_ticks():
    obs = [(1, 1), (5, 1), (10, 0), (20, 1)]
    km = kaplan_meier(obs, time_ticks=[0, 5, 10, 20])
    # patients with time >= tick
    assert km["at_risk_at_ticks"] == [4, 3, 2, 1]


def test_kaplan_meier_drops_invalid_times():
    # Intentionally malformed observations to verify they are filtered out.
    km = kaplan_meier([(10, 1), (None, 1), (-3, 1), ("bad", 0), (5, 1)])  # type: ignore[list-item]
    assert km["n_patients"] == 2


# --- logrank_test ------------------------------------------------------------


def test_logrank_freireich():
    lr = logrank_test({"6-MP": FREIREICH_6MP, "placebo": FREIREICH_PLACEBO})
    assert lr["df"] == 1
    assert math.isclose(lr["chi_square"], 16.79, abs_tol=0.02)
    assert lr["p_value"] < 1e-4
    oe = {g["group"]: g for g in lr["group_observed_expected"]}
    assert oe["6-MP"]["observed"] == 9
    assert math.isclose(oe["6-MP"]["expected"], 19.25, abs_tol=0.05)


def test_logrank_three_groups_df():
    g1 = [(t, 1) for t in (1, 2, 3, 4, 5)]
    g2 = [(t, 1) for t in (3, 4, 5, 6, 7)]
    g3 = [(t, 1) for t in (5, 6, 7, 8, 9)]
    lr = logrank_test({"a": g1, "b": g2, "c": g3})
    assert lr["df"] == 2
    assert lr["p_value"] is not None


def test_logrank_identical_groups_not_significant():
    g = [(1, 1), (2, 1), (3, 0), (4, 1), (5, 0)]
    lr = logrank_test({"a": list(g), "b": list(g)})
    # Identical groups => no separation => large p-value.
    assert lr["p_value"] > 0.5


def test_logrank_requires_two_groups():
    lr = logrank_test({"only": [(1, 1), (2, 1)]})
    assert lr["p_value"] is None
    assert "reason" in lr


def test_logrank_no_events():
    lr = logrank_test({"a": [(1, 0), (2, 0)], "b": [(3, 0), (4, 0)]})
    assert lr["p_value"] is None
    assert "reason" in lr
