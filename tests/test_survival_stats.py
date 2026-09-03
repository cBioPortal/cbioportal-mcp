"""Tests for the pure-Python survival statistics (Kaplan-Meier + log-rank)."""

import math

import pytest

from cbioportal_mcp.survival_stats import (
    chi_square_sf,
    downsample_curve,
    kaplan_meier,
    logrank_test,
    normal_cdf,
    normal_ppf,
)

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
    # First curve point is always (0, 1.0), with the band collapsed onto it.
    assert km["curve"][0] == {
        "time": 0.0,
        "survival": 1.0,
        "std_err": 0.0,
        "ci_lower": 1.0,
        "ci_upper": 1.0,
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
    assert km["curve"] == [
        {
            "time": 0.0,
            "survival": 1.0,
            "std_err": 0.0,
            "ci_lower": 1.0,
            "ci_upper": 1.0,
            "at_risk": 0,
            "events": 0,
            "censored": 0,
        }
    ]


def test_kaplan_meier_at_risk_at_ticks():
    obs = [(1, 1), (5, 1), (10, 0), (20, 1)]
    km = kaplan_meier(obs, time_ticks=[0, 5, 10, 20])
    # patients with time >= tick
    assert km["at_risk_at_ticks"] == [4, 3, 2, 1]


def test_kaplan_meier_drops_invalid_times():
    # Intentionally malformed observations to verify they are filtered out.
    km = kaplan_meier([(10, 1), (None, 1), (-3, 1), ("bad", 0), (5, 1)])  # type: ignore[list-item]
    assert km["n_patients"] == 2


# --- normal_ppf --------------------------------------------------------------


@pytest.mark.parametrize(
    "p,expected",
    [
        (0.975, 1.959963984540054),  # the 95% two-sided critical value
        (0.995, 2.5758293035489004),  # 99%
        (0.95, 1.6448536269514722),
        (0.5, 0.0),
        (0.025, -1.959963984540054),
    ],
)
def test_normal_ppf_known_quantiles(p, expected):
    assert math.isclose(normal_ppf(p), expected, abs_tol=1e-9)


def test_normal_ppf_inverts_the_cdf():
    for p in (0.001, 0.1, 0.3, 0.62, 0.9, 0.999):
        assert math.isclose(normal_cdf(normal_ppf(p)), p, abs_tol=1e-12)


@pytest.mark.parametrize("p", [0.0, 1.0, -0.1, 1.5])
def test_normal_ppf_rejects_out_of_range(p):
    with pytest.raises(ValueError):
        normal_ppf(p)


# --- Kaplan-Meier confidence band --------------------------------------------


def test_greenwood_std_err_matches_published_freireich_table():
    """Greenwood SEs for the 6-MP arm, against the standard published table.

    S(6)=0.857 (se 0.0764), S(7)=0.807 (se 0.0869), S(10)=0.753 (se 0.0963) --
    the values this dataset is tabulated with wherever it is worked through.
    """
    curve = {p["time"]: p for p in kaplan_meier(FREIREICH_6MP)["curve"]}
    for t, survival, se in [(6, 0.8571, 0.0764), (7, 0.8067, 0.0869), (10, 0.7529, 0.0963)]:
        assert math.isclose(curve[t]["survival"], survival, abs_tol=5e-5)
        assert math.isclose(curve[t]["std_err"], se, abs_tol=5e-5)


def test_confidence_band_hand_computed():
    """Log-log limits at t=1 of a curve small enough to check by hand.

    n=4, one event at t=1 => S=0.75; Greenwood's sum is 1/(4*3)=0.0833333, so
    se(S) = 0.75*sqrt(0.0833333) = 0.2165064. The log-log limits are
    S**exp(+/- c) with c = z*sqrt(0.0833333)/|ln 0.75| = 1.9666.
    """
    km = kaplan_meier([(1, 1), (2, 0), (3, 1), (4, 1)])
    pt = {p["time"]: p for p in km["curve"]}[1]
    assert math.isclose(pt["survival"], 0.75)
    assert math.isclose(pt["std_err"], 0.2165064, abs_tol=1e-7)
    assert math.isclose(pt["ci_lower"], 0.127947, abs_tol=1e-6)
    assert math.isclose(pt["ci_upper"], 0.960549, abs_tol=1e-6)


def test_confidence_band_brackets_the_estimate_and_stays_in_unit_interval():
    km = kaplan_meier(FREIREICH_6MP)
    for p in km["curve"]:
        assert 0.0 <= p["ci_lower"] <= p["survival"] <= p["ci_upper"] <= 1.0


def test_confidence_band_widens_as_the_at_risk_count_falls():
    """The point of the band: late estimates are visibly less certain.

    Widths are compared on this dataset only. Greenwood's sum accumulates, but
    the width on the probability scale is not monotone in general -- as S
    approaches 0 the band is squeezed against the floor and narrows again.
    """
    events = [p for p in kaplan_meier(FREIREICH_6MP)["curve"] if p["events"]]
    widths = [p["ci_upper"] - p["ci_lower"] for p in events]
    assert widths[-1] > 1.4 * widths[0]
    assert widths[-1] > widths[len(widths) // 2] > widths[0]


def test_higher_confidence_gives_a_wider_band():
    obs = FREIREICH_6MP
    p95 = kaplan_meier(obs, conf_level=0.95)["curve"][-1]
    p99 = kaplan_meier(obs, conf_level=0.99)["curve"][-1]
    assert p99["ci_lower"] < p95["ci_lower"]
    assert p99["ci_upper"] > p95["ci_upper"]
    # The point estimate and its SE do not depend on the coverage.
    assert p99["survival"] == p95["survival"]
    assert p99["std_err"] == p95["std_err"]


def test_conf_level_is_echoed_back():
    assert kaplan_meier([(1, 1)])["conf_level"] == 0.95
    assert kaplan_meier([(1, 1)], conf_level=0.9)["conf_level"] == 0.9


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.5, 1.2])
def test_kaplan_meier_rejects_bad_conf_level(bad):
    with pytest.raises(ValueError):
        kaplan_meier([(1, 1)], conf_level=bad)


def test_band_collapses_where_the_estimator_is_degenerate():
    # All censored => S stays 1.0 and the log-log transform is undefined.
    for p in kaplan_meier([(5, 0), (10, 0)])["curve"]:
        assert (p["ci_lower"], p["ci_upper"]) == (1.0, 1.0)
    # Everyone dies => S hits exactly 0 and Greenwood's term diverges; the
    # interval collapses rather than producing a NaN or blowing up.
    last = kaplan_meier([(1, 1), (2, 1)])["curve"][-1]
    assert last["survival"] == 0.0
    assert (last["ci_lower"], last["ci_upper"]) == (0.0, 0.0)
    assert last["std_err"] == 0.0


def test_band_survives_a_tie_that_removes_everyone_at_risk():
    # Two events at the same time as the last two at risk: n == d exactly.
    curve = kaplan_meier([(1, 1), (5, 1), (5, 1)])["curve"]
    assert all(math.isfinite(p["ci_lower"]) and math.isfinite(p["ci_upper"]) for p in curve)
    assert curve[-1]["survival"] == 0.0


def test_censoring_alone_does_not_widen_the_band():
    """A censor event carries no information about S, only about who is left."""
    with_censor = kaplan_meier([(1, 1), (2, 0), (3, 0), (10, 1)])["curve"]
    at_1, at_2 = with_censor[1], with_censor[2]
    assert at_2["censored"] == 1
    assert at_2["survival"] == at_1["survival"]
    assert at_2["ci_lower"] == at_1["ci_lower"]
    assert at_2["ci_upper"] == at_1["ci_upper"]


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


# ---------------------------------------------------------------------------
# Curve downsampling
# ---------------------------------------------------------------------------


def _big_curve(n_patients=2000):
    """A KM curve with one distinct event time per patient."""
    obs = [(float(i + 1), 1 if i % 3 else 0) for i in range(n_patients)]
    return kaplan_meier(obs)


def test_downsample_leaves_short_curves_alone():
    km = kaplan_meier([(1, 1), (2, 0), (3, 1)])
    out = downsample_curve(km["curve"], 200)
    assert out == km["curve"]


def test_downsample_respects_the_cap():
    curve = _big_curve()["curve"]
    assert len(curve) > 1000
    out = downsample_curve(curve, 200)
    assert len(out) == 200


def test_downsample_keeps_the_endpoints():
    curve = _big_curve()["curve"]
    out = downsample_curve(curve, 200)
    assert out[0] == curve[0]
    assert out[0]["time"] == 0.0 and out[0]["survival"] == 1.0
    assert out[-1]["time"] == curve[-1]["time"]
    assert out[-1]["survival"] == curve[-1]["survival"]


def test_downsample_preserves_exact_survival_and_band_at_reported_times():
    km = _big_curve()
    by_time = {p["time"]: p for p in km["curve"]}
    for p in downsample_curve(km["curve"], 200):
        original = by_time[p["time"]]
        assert p["survival"] == original["survival"]
        assert p["ci_lower"] == original["ci_lower"]
        assert p["ci_upper"] == original["ci_upper"]
        assert p["at_risk"] == original["at_risk"]


def test_downsample_conserves_event_and_censor_totals():
    km = _big_curve()
    out = downsample_curve(km["curve"], 200)
    assert sum(p["events"] for p in out) == km["n_events"]
    assert sum(p["censored"] for p in out) == km["n_censored"]


def test_downsample_is_monotone_in_time_and_survival():
    out = downsample_curve(_big_curve()["curve"], 200)
    times = [p["time"] for p in out]
    survivals = [p["survival"] for p in out]
    assert times == sorted(times)
    assert len(set(times)) == len(times)
    assert all(b <= a for a, b in zip(survivals, survivals[1:]))


def test_downsample_tracks_the_full_curve_closely():
    """Binning is a coarser grid, not a different estimator.

    Between two reported times the binned curve holds the earlier value while
    the true curve walks down to the later one, so the error at any time is
    bounded by the drop across the bin containing it -- and nothing else.
    """
    km = _big_curve()
    out = downsample_curve(km["curve"], 200)
    worst = 0.0
    i = 0
    for p in km["curve"]:
        while i + 1 < len(out) and out[i + 1]["time"] <= p["time"]:
            i += 1
        worst = max(worst, abs(out[i]["survival"] - p["survival"]))
    bin_drops = [a["survival"] - b["survival"] for a, b in zip(out, out[1:])]
    assert worst <= max(bin_drops) + 1e-12
    # And on this curve no single bin swallows a large part of the fall.
    assert max(bin_drops) < 0.05


def test_downsample_handles_a_cap_larger_than_the_curve():
    curve = kaplan_meier([(1, 1), (2, 1)])["curve"]
    assert downsample_curve(curve, 1000) == curve


@pytest.mark.parametrize("cap", [0, 1])
def test_downsample_degenerate_caps_return_the_curve(cap):
    curve = _big_curve(50)["curve"]
    assert downsample_curve(curve, cap) == curve
