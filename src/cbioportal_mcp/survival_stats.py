"""Pure-Python survival statistics for the Kaplan-Meier UI app.

This module is intentionally dependency-free (standard library only): no
``scipy``/``lifelines``. It operates on plain ``(time, event)`` observations so
it can be unit-tested in isolation, with no cBioPortal/ClickHouse knowledge.

- ``kaplan_meier`` — the product-limit estimator (step curve, at-risk counts,
  median survival, censor marks, pointwise confidence band).
- ``logrank_test`` — the multivariate log-rank test for 2+ groups
  (chi-square statistic, degrees of freedom, p-value).
- ``stratified_logrank_test`` — the same test with observed-minus-expected
  events and their covariance summed within strata (e.g. cancer type), so a
  comparison across a mixed cohort is not driven by differences between strata.
- ``chi_square_sf`` — the chi-square survival function (upper-tail p-value),
  via the regularized upper incomplete gamma function.
- ``normal_ppf`` — the standard normal quantile function, for the band's
  critical value.
- ``downsample_curve`` — bin a step curve down to a transport-sized number of
  points, for studies with thousands of distinct event times.

An "observation" is ``(time, event)`` where ``time >= 0`` is the follow-up time
and ``event`` is ``1`` if the event (e.g. death) was observed at ``time`` or
``0`` if the observation was censored at ``time``.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import Counter, defaultdict
from typing import Iterable, Mapping, Sequence

Observation = tuple[float, int]


# ---------------------------------------------------------------------------
# Chi-square survival function (no scipy)
# ---------------------------------------------------------------------------

# Iteration limits for the incomplete-gamma series / continued fraction.
_GAMMA_MAX_ITER = 300
_GAMMA_EPS = 3.0e-12


def _gamma_p_series(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x) via its series expansion.

    Accurate for ``x < a + 1``. See Numerical Recipes, "gser".
    """
    if x <= 0.0:
        return 0.0
    ap = a
    total = 1.0 / a
    term = total
    for _ in range(_GAMMA_MAX_ITER):
        ap += 1.0
        term *= x / ap
        total += term
        if abs(term) < abs(total) * _GAMMA_EPS:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gamma_q_continued_fraction(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x) via a continued fraction.

    Accurate for ``x >= a + 1``. See Numerical Recipes, "gcf" (Lentz's method).
    """
    tiny = 1.0e-30
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, _GAMMA_MAX_ITER):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _GAMMA_EPS:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def regularized_gamma_q(a: float, x: float) -> float:
    """Regularized upper incomplete gamma function Q(a, x) = 1 - P(a, x)."""
    if x < 0.0 or a <= 0.0:
        raise ValueError("regularized_gamma_q requires a > 0 and x >= 0")
    if x == 0.0:
        return 1.0
    if x < a + 1.0:
        return 1.0 - _gamma_p_series(a, x)
    return _gamma_q_continued_fraction(a, x)


def chi_square_sf(x: float, df: int) -> float:
    """Survival function (upper tail) of the chi-square distribution.

    Returns P(X > x) for X ~ chi-square with ``df`` degrees of freedom — i.e.
    the p-value for a chi-square statistic of ``x``.
    """
    if df < 1:
        raise ValueError("df must be >= 1")
    if x <= 0.0:
        return 1.0
    return regularized_gamma_q(df / 2.0, x / 2.0)


# ---------------------------------------------------------------------------
# Standard normal quantile (no scipy)
# ---------------------------------------------------------------------------


def normal_cdf(x: float) -> float:
    """Standard normal CDF, via the stdlib error function."""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def normal_ppf(p: float) -> float:
    """Standard normal quantile function (inverse CDF) for ``0 < p < 1``.

    Bisection on the (strictly increasing) CDF rather than a rational
    approximation: the closed forms need a page of magic constants, and this is
    called once per curve, so the ~60 ``erfc`` evaluations cost nothing. The
    bracket is +/-40 sigma, well outside the range where the CDF is
    distinguishable from 0 or 1 in double precision.
    """
    if not 0.0 < p < 1.0:
        raise ValueError("normal_ppf requires 0 < p < 1")
    if p == 0.5:
        return 0.0
    lo, hi = -40.0, 40.0
    # Each step halves an 80-wide bracket; 60 steps is past double precision.
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if normal_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# Kaplan-Meier estimator
# ---------------------------------------------------------------------------


def _clean_observations(observations: Iterable[Observation]) -> list[Observation]:
    """Drop observations with missing/negative times and coerce the event flag."""
    cleaned: list[Observation] = []
    for time, event in observations:
        if time is None:
            continue
        try:
            t = float(time)
        except (TypeError, ValueError):
            continue
        if math.isnan(t) or t < 0.0:
            continue
        cleaned.append((t, 1 if event else 0))
    return cleaned


def _greenwood_interval(survival: float, greenwood_sum: float, z: float) -> tuple[float, float]:
    """Pointwise confidence limits for one Kaplan-Meier step point.

    ``greenwood_sum`` is Greenwood's sum ``sum(d_i / (n_i * (n_i - d_i)))`` over
    event times up to this point, so the variance of ``S(t)`` is
    ``S(t)**2 * greenwood_sum``.

    Uses the log-log (log-minus-log) transform: the limits are those of
    ``log(-log S)`` mapped back through ``S = exp(-exp(.))``, which keeps them
    inside ``(0, 1)`` without clipping. A plain ``S +/- z * se`` interval runs
    outside the unit interval whenever ``S`` is near 0 or 1 -- exactly where a
    survival band is read hardest, in the sparse tail.

    ``S = 1`` (no events yet) and ``S = 0`` (no survivors) are degenerate: the
    transform is undefined and the estimator carries no information about where
    the truth might be, so the interval collapses onto the point estimate --
    the same convention R's ``survfit`` uses.
    """
    if survival >= 1.0:
        return 1.0, 1.0
    if survival <= 0.0:
        return 0.0, 0.0
    if greenwood_sum <= 0.0:
        return survival, survival
    log_s = math.log(survival)
    # c = z * se(log(-log S)); se is sqrt(greenwood_sum) / |log S|.
    c = z * math.sqrt(greenwood_sum) / -log_s
    # S is decreasing in log(-log S), so +c gives the LOWER survival limit.
    return survival ** math.exp(c), survival ** math.exp(-c)


def kaplan_meier(
    observations: Iterable[Observation],
    time_ticks: Sequence[float] | None = None,
    conf_level: float = 0.95,
) -> dict:
    """Compute the Kaplan-Meier (product-limit) estimate for one group.

    Args:
        observations: iterable of ``(time, event)`` pairs.
        time_ticks: optional times at which to report the number-at-risk
            (for the at-risk table under the plot). Defaults to ``[]``.
        conf_level: coverage of the pointwise confidence band (default 0.95).

    Returns a dict with:
        - ``n_patients`` / ``n_events`` / ``n_censored``
        - ``max_time``
        - ``median_survival`` (float, or ``None`` if not reached)
        - ``conf_level``: the band's coverage, echoed back.
        - ``curve``: list of
          ``{time, survival, std_err, ci_lower, ci_upper, at_risk, events, censored}``
          step points, always starting at ``{time: 0, survival: 1.0}``.
          ``std_err`` is Greenwood's standard error of ``S(t)``; the limits are
          pointwise (see ``_greenwood_interval``), not a simultaneous band, so
          two curves whose bands overlap at some time have *not* thereby been
          tested for a difference -- that is what ``logrank_test`` is for.
        - ``at_risk_at_ticks``: number at risk at each requested tick time.
    """
    if not 0.0 < conf_level < 1.0:
        raise ValueError("conf_level must be between 0 and 1 (exclusive)")
    z = normal_ppf(0.5 + conf_level / 2.0)
    obs = _clean_observations(observations)
    n = len(obs)
    result: dict = {
        "n_patients": n,
        "n_events": sum(e for _, e in obs),
        "n_censored": sum(1 for _, e in obs if not e),
        "max_time": max((t for t, _ in obs), default=0.0),
        "median_survival": None,
        "conf_level": conf_level,
        "curve": [
            {
                "time": 0.0,
                "survival": 1.0,
                "std_err": 0.0,
                "ci_lower": 1.0,
                "ci_upper": 1.0,
                "at_risk": n,
                "events": 0,
                "censored": 0,
            }
        ],
        "at_risk_at_ticks": [],
    }
    if n == 0:
        return result

    # Aggregate events/censors by distinct time.
    events_at: dict[float, int] = defaultdict(int)
    censored_at: dict[float, int] = defaultdict(int)
    for t, e in obs:
        if e:
            events_at[t] += 1
        else:
            censored_at[t] += 1

    distinct_times = sorted(set(events_at) | set(censored_at))
    survival = 1.0
    at_risk = n
    median = None
    greenwood_sum = 0.0  # running sum(d / (n * (n - d))) over event times
    for t in distinct_times:
        d = events_at.get(t, 0)
        c = censored_at.get(t, 0)
        at_risk_here = at_risk  # number with time >= t, before removals at t
        if d > 0 and at_risk_here > 0:
            survival *= 1.0 - d / at_risk_here
            # n == d means no survivors past t: survival is exactly 0 from here
            # on and the Greenwood term diverges, so leave the sum alone and let
            # the S == 0 branch of _greenwood_interval handle it.
            if at_risk_here > d:
                greenwood_sum += d / (at_risk_here * (at_risk_here - d))
        ci_lower, ci_upper = _greenwood_interval(survival, greenwood_sum, z)
        result["curve"].append(
            {
                "time": t,
                "survival": survival,
                "std_err": survival * math.sqrt(greenwood_sum),
                "ci_lower": ci_lower,
                "ci_upper": ci_upper,
                "at_risk": at_risk_here,
                "events": d,
                "censored": c,
            }
        )
        # Median: first event time at which survival drops to <= 0.5.
        if median is None and d > 0 and survival <= 0.5:
            median = t
        at_risk -= d + c

    result["median_survival"] = median
    if time_ticks:
        result["at_risk_at_ticks"] = [sum(1 for t, _ in obs if t >= tick) for tick in time_ticks]
    return result


def downsample_curve(curve: Sequence[dict], max_points: int) -> list[dict]:
    """Bin a Kaplan-Meier step curve down to at most ``max_points`` points.

    A study with tens of thousands of patients has one step per distinct
    follow-up time -- thousands of points per curve -- which is far more than a
    plot needs and more than a tool payload should carry.

    The curve is cut into roughly equal-sized runs of consecutive points and
    each run is represented by its *last* point, so:

    - ``time`` / ``survival`` / ``ci_lower`` / ``ci_upper`` / ``std_err`` /
      ``at_risk`` are exact values of the estimator at the reported time -- no
      interpolation or smoothing happens, the step function is just reported on
      a coarser time grid.
    - ``events`` and ``censored`` are summed over the run, so they still total
      the group's ``n_events`` / ``n_censored``. They now describe the interval
      ending at ``time`` rather than that instant alone.

    Binning by *count* rather than by elapsed time keeps the resolution where
    the events are: stretches dense in distinct times get narrow bins.

    The ``t = 0`` anchor point is always kept, and so is the final point (the
    end of follow-up), so the curve's extent is unchanged.
    """
    n = len(curve)
    if max_points < 2 or n <= max_points:
        return [dict(p) for p in curve]

    rest = curve[1:]
    m = len(rest)
    n_bins = max_points - 1
    out = [dict(curve[0])]
    start = 0
    for i in range(1, n_bins + 1):
        end = (i * m) // n_bins
        if end <= start:
            continue
        chunk = rest[start:end]
        point = dict(chunk[-1])
        point["events"] = sum(p["events"] for p in chunk)
        point["censored"] = sum(p["censored"] for p in chunk)
        out.append(point)
        start = end
    return out


# ---------------------------------------------------------------------------
# Log-rank test (multivariate, 2+ groups)
# ---------------------------------------------------------------------------


def _solve_quadratic_form(cov: list[list[float]], z: list[float]) -> float | None:
    """Return ``z^T cov^{-1} z`` by solving ``cov x = z`` (Gaussian elimination).

    Returns ``None`` if ``cov`` is singular / not positive definite.
    """
    k = len(z)
    if k == 0:
        return None
    # Augmented matrix [cov | z]
    m = [list(cov[i]) + [z[i]] for i in range(k)]
    for col in range(k):
        # Partial pivot
        pivot_row = max(range(col, k), key=lambda r: abs(m[r][col]))
        if abs(m[pivot_row][col]) < 1e-12:
            return None
        m[col], m[pivot_row] = m[pivot_row], m[col]
        pivot = m[col][col]
        for r in range(k):
            if r == col:
                continue
            factor = m[r][col] / pivot
            for c in range(col, k + 1):
                m[r][c] -= factor * m[col][c]
    x = [m[i][k] / m[i][i] for i in range(k)]
    return sum(z[i] * x[i] for i in range(k))


def _logrank_components(
    groups: Sequence[Sequence[Observation]],
) -> tuple[list[float], list[float], list[list[float]]]:
    """Observed events, expected events and the (k-1)x(k-1) covariance of O - E.

    ``groups`` are cleaned observation lists in a fixed order; an empty group simply
    contributes nothing. Swept over the distinct event times with each group's times
    sorted once, so the cost is O(T * k * log n) rather than a scan of every
    observation at every event time.
    """
    k = len(groups)
    dim = max(k - 1, 0)
    observed = [0.0] * k
    expected = [0.0] * k
    cov = [[0.0 for _ in range(dim)] for _ in range(dim)]
    sorted_times = [sorted(t for t, _ in obs) for obs in groups]
    events_at = [Counter(t for t, e in obs if e) for obs in groups]
    event_times = sorted(set().union(*(set(c) for c in events_at))) if groups else []
    for t in event_times:
        n_g = [len(times) - bisect_left(times, t) for times in sorted_times]
        d_g = [events_at[g].get(t, 0) for g in range(k)]
        n = sum(n_g)
        d = sum(d_g)
        if d == 0:
            continue
        for g in range(k):
            observed[g] += d_g[g]
            # E = d * n_g / n is defined whenever someone is at risk -- including the
            # last patient dying alone (n == 1), where it equals O. Skipping it there
            # left O - E unbalanced, which small strata hit constantly.
            expected[g] += d * n_g[g] / n
        if n <= 1:
            continue  # the variance term d(n-d)/(n-1) is 0 when n == d == 1
        var_factor = d * (n - d) / (n - 1)
        for g in range(dim):
            frac_g = n_g[g] / n
            cov[g][g] += var_factor * frac_g * (1.0 - frac_g)
            for h in range(g + 1, dim):
                frac_h = n_g[h] / n
                cov[g][h] -= var_factor * frac_g * frac_h
                cov[h][g] = cov[g][h]
    return observed, expected, cov


def _logrank_statistic(
    names: Sequence[str],
    observed: Sequence[float],
    expected: Sequence[float],
    cov: Sequence[Sequence[float]],
    result: dict,
) -> dict:
    k = len(names)
    result["group_observed_expected"] = [
        {"group": names[g], "observed": observed[g], "expected": expected[g]} for g in range(k)
    ]
    z = [observed[g] - expected[g] for g in range(k - 1)]
    chi_square = _solve_quadratic_form([list(row) for row in cov], z)
    if chi_square is None or chi_square < 0.0:
        result["reason"] = "Covariance matrix is singular; log-rank statistic is undefined."
        return result
    result["chi_square"] = chi_square
    result["df"] = k - 1
    result["p_value"] = chi_square_sf(chi_square, k - 1)
    return result


def logrank_test(
    groups: "dict[str, Iterable[Observation]] | Sequence[Iterable[Observation]]",
) -> dict:
    """Multivariate log-rank test across 2+ groups.

    Args:
        groups: mapping of ``group_name -> observations`` (or a sequence of
            observation iterables). Groups with no patients are ignored.

    Returns a dict with ``test`` ('log-rank'), ``chi_square``, ``df``,
    ``p_value`` (``None`` if undefined), and ``group_observed_expected``
    (per-group observed vs. expected events). When fewer than two non-empty
    groups are supplied, ``p_value`` is ``None`` and a ``reason`` is given.
    """
    if isinstance(groups, dict):
        items = list(groups.items())
    else:
        items = [(str(i), g) for i, g in enumerate(groups)]

    named = [(name, _clean_observations(obs)) for name, obs in items]
    named = [(name, obs) for name, obs in named if obs]

    result: dict = {
        "test": "log-rank",
        "chi_square": None,
        "df": None,
        "p_value": None,
        "group_observed_expected": [],
    }
    if len(named) < 2:
        result["reason"] = "Log-rank test requires at least two non-empty groups."
        return result
    if not any(e for _, obs in named for _, e in obs):
        result["reason"] = "No events observed in any group; log-rank is undefined."
        return result

    observed, expected, cov = _logrank_components([obs for _, obs in named])
    return _logrank_statistic([name for name, _ in named], observed, expected, cov, result)


def stratified_logrank_test(
    strata: "Mapping[str, Mapping[str, Iterable[Observation]]]",
    order: Sequence[str] | None = None,
) -> dict:
    """Log-rank test of 2+ groups stratified by a nuisance factor.

    ``strata`` maps ``stratum -> {group_name: observations}``. Within each stratum the
    usual log-rank observed and expected event counts and covariance are computed over
    that stratum's own risk sets; they are then summed across strata and the chi-square
    ``(O - E)' V^-1 (O - E)`` is formed on the totals (R's
    ``survdiff(Surv(t, e) ~ group + strata(s))``). Groups compare only with patients in
    the same stratum, so a group that is merely over-represented in a good-prognosis
    stratum does not look protective.

    Returns the ``logrank_test`` keys plus ``test`` = 'stratified log-rank',
    ``n_strata`` and ``n_informative_strata`` (strata where at least two groups have
    patients and an event occurred). ``order`` fixes the group order of
    ``group_observed_expected`` (default: order of first appearance).
    """
    cleaned: dict[str, dict[str, list[Observation]]] = {}
    for stratum, groups in strata.items():
        cleaned[str(stratum)] = {
            str(name): _clean_observations(obs) for name, obs in groups.items()
        }
    names: list[str] = []
    for groups in cleaned.values():
        for name, obs in groups.items():
            if obs and name not in names:
                names.append(name)
    if order is not None:
        names = [str(n) for n in order if str(n) in names] + [
            n for n in names if n not in {str(o) for o in order}
        ]

    result: dict = {
        "test": "stratified log-rank",
        "chi_square": None,
        "df": None,
        "p_value": None,
        "group_observed_expected": [],
        "n_strata": len(cleaned),
        "n_informative_strata": 0,
    }
    if len(names) < 2:
        result["reason"] = "Log-rank test requires at least two non-empty groups."
        return result

    k = len(names)
    observed = [0.0] * k
    expected = [0.0] * k
    cov = [[0.0 for _ in range(k - 1)] for _ in range(k - 1)]
    for groups in cleaned.values():
        ordered = [groups.get(name, []) for name in names]
        if sum(1 for obs in ordered if obs) >= 2 and any(e for obs in ordered for _, e in obs):
            result["n_informative_strata"] += 1
        o, e, v = _logrank_components(ordered)
        for g in range(k):
            observed[g] += o[g]
            expected[g] += e[g]
        for g in range(k - 1):
            for h in range(k - 1):
                cov[g][h] += v[g][h]
    if not result["n_informative_strata"]:
        result["reason"] = (
            "No stratum contains two groups with an event; the stratified log-rank test is "
            "undefined."
        )
        result["group_observed_expected"] = [
            {"group": names[g], "observed": observed[g], "expected": expected[g]} for g in range(k)
        ]
        return result
    return _logrank_statistic(names, observed, expected, cov, result)
