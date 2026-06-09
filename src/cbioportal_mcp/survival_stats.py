"""Pure-Python survival statistics for the Kaplan-Meier UI app.

This module is intentionally dependency-free (standard library only): no
``scipy``/``lifelines``. It operates on plain ``(time, event)`` observations so
it can be unit-tested in isolation, with no cBioPortal/ClickHouse knowledge.

- ``kaplan_meier`` — the product-limit estimator (step curve, at-risk counts,
  median survival, censor marks).
- ``logrank_test`` — the multivariate log-rank test for 2+ groups
  (chi-square statistic, degrees of freedom, p-value).
- ``chi_square_sf`` — the chi-square survival function (upper-tail p-value),
  via the regularized upper incomplete gamma function.

An "observation" is ``(time, event)`` where ``time >= 0`` is the follow-up time
and ``event`` is ``1`` if the event (e.g. death) was observed at ``time`` or
``0`` if the observation was censored at ``time``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, Sequence

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


def kaplan_meier(
    observations: Iterable[Observation], time_ticks: Sequence[float] | None = None
) -> dict:
    """Compute the Kaplan-Meier (product-limit) estimate for one group.

    Args:
        observations: iterable of ``(time, event)`` pairs.
        time_ticks: optional times at which to report the number-at-risk
            (for the at-risk table under the plot). Defaults to ``[]``.

    Returns a dict with:
        - ``n_patients`` / ``n_events`` / ``n_censored``
        - ``max_time``
        - ``median_survival`` (float, or ``None`` if not reached)
        - ``curve``: list of ``{time, survival, at_risk, events, censored}``
          step points, always starting at ``{time: 0, survival: 1.0}``.
        - ``at_risk_at_ticks``: number at risk at each requested tick time.
    """
    obs = _clean_observations(observations)
    n = len(obs)
    result: dict = {
        "n_patients": n,
        "n_events": sum(e for _, e in obs),
        "n_censored": sum(1 for _, e in obs if not e),
        "max_time": max((t for t, _ in obs), default=0.0),
        "median_survival": None,
        "curve": [{"time": 0.0, "survival": 1.0, "at_risk": n, "events": 0, "censored": 0}],
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
    for t in distinct_times:
        d = events_at.get(t, 0)
        c = censored_at.get(t, 0)
        at_risk_here = at_risk  # number with time >= t, before removals at t
        if d > 0 and at_risk_here > 0:
            survival *= 1.0 - d / at_risk_here
        result["curve"].append(
            {
                "time": t,
                "survival": survival,
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

    k = len(named)
    # Pooled distinct event times.
    event_times: set[float] = set()
    for _, obs in named:
        for t, e in obs:
            if e:
                event_times.add(t)
    if not event_times:
        result["reason"] = "No events observed in any group; log-rank is undefined."
        return result

    observed = [0.0] * k
    expected = [0.0] * k
    # Reduced covariance over the first k-1 groups (full matrix is rank k-1).
    dim = k - 1
    cov = [[0.0 for _ in range(dim)] for _ in range(dim)]

    for t in sorted(event_times):
        n_g = [sum(1 for ot, _ in obs if ot >= t) for _, obs in named]
        d_g = [sum(1 for ot, oe in obs if ot == t and oe) for _, obs in named]
        n = sum(n_g)
        d = sum(d_g)
        if n <= 1 or d == 0:
            # Still accumulate observed events for reporting.
            for g in range(k):
                observed[g] += d_g[g]
            continue
        for g in range(k):
            observed[g] += d_g[g]
            expected[g] += d * n_g[g] / n
        var_factor = d * (n - d) / (n - 1)
        for g in range(dim):
            frac_g = n_g[g] / n
            cov[g][g] += var_factor * frac_g * (1.0 - frac_g)
            for h in range(g + 1, dim):
                frac_h = n_g[h] / n
                cov[g][h] -= var_factor * frac_g * frac_h
                cov[h][g] = cov[g][h]

    result["group_observed_expected"] = [
        {"group": named[g][0], "observed": observed[g], "expected": expected[g]} for g in range(k)
    ]

    z = [observed[g] - expected[g] for g in range(dim)]
    chi_square = _solve_quadratic_form(cov, z)
    if chi_square is None or chi_square < 0.0:
        result["reason"] = "Covariance matrix is singular; log-rank statistic is undefined."
        return result

    df = k - 1
    result["chi_square"] = chi_square
    result["df"] = df
    result["p_value"] = chi_square_sf(chi_square, df)
    return result
