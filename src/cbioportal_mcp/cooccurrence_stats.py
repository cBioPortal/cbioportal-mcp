"""Pure-Python statistics for the alteration co-occurrence UI app.

Like ``survival_stats.py``, this module is intentionally dependency-free
(standard library only): no ``scipy``. It works on plain 2x2 contingency counts
so it can be unit-tested in isolation, with no cBioPortal/ClickHouse knowledge.

For a pair of genes, the 2x2 table over a shared sample set is::

                 B altered    B not altered
    A altered        a              b
    A not altered    c              d

- ``fisher_exact_two_sided(a, b, c, d)`` — the two-sided Fisher exact test
  p-value (same convention as ``scipy.stats.fisher_exact``: sum the
  hypergeometric probabilities of all tables, with the observed margins, that
  are no more likely than the observed one).
- ``log2_odds_ratio(a, b, c, d)`` — log2 odds ratio with the Haldane-Anscombe
  (+0.5) correction so it is always finite (positive => co-occurrence,
  negative => mutual exclusivity).
- ``benjamini_hochberg(pvalues)`` — Benjamini-Hochberg FDR q-values.

Stratified (2x2xK) association, for a confounder such as tumour type -- one 2x2
table per stratum, all testing the same pair:

- ``exact_conditional_test(tables)`` — the exact conditional test of independence
  given every stratum's margins (R's ``mantelhaen.test(exact = TRUE)``); with one
  stratum it is Fisher's exact test.
- ``cmh_test(tables)`` — the Cochran-Mantel-Haenszel chi-square with continuity
  correction (R's ``mantelhaen.test()``).
- ``mantel_haenszel_odds_ratio(tables)`` — the common odds ratio across strata.
- ``stratified_association_test(tables)`` — picks between the two tests by the
  Mantel-Fleiss criterion and reports which one ran.
"""

from __future__ import annotations

import math
from typing import Sequence

from cbioportal_mcp.survival_stats import chi_square_sf

# Tolerance when comparing log-probabilities for the two-sided test, so a table
# whose probability equals the observed one (up to floating-point error) is
# included rather than dropped.
_LOGP_TOL = 1e-7


def _log_comb(n: int, k: int) -> float:
    """Natural log of the binomial coefficient C(n, k)."""
    if k < 0 or k > n:
        return -math.inf
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact test p-value for the 2x2 table [[a, b], [c, d]].

    Returns 1.0 for a degenerate (all-zero / empty-margin) table, where the test
    is undefined and there is no evidence of association.
    """
    a, b, c, d = int(a), int(b), int(c), int(d)
    if min(a, b, c, d) < 0:
        raise ValueError("Contingency counts must be non-negative.")
    row1 = a + b
    row2 = c + d
    col1 = a + c
    col2 = b + d
    n = a + b + c + d
    # Undefined when any margin is empty (one variable is constant): no association.
    if n == 0 or row1 == 0 or row2 == 0 or col1 == 0 or col2 == 0:
        return 1.0

    log_denom = _log_comb(n, col1)

    def log_prob(x: int) -> float:
        # Hypergeometric prob of top-left cell == x, holding the margins fixed.
        return _log_comb(row1, x) + _log_comb(row2, col1 - x) - log_denom

    log_p_obs = log_prob(a)
    lo = max(0, col1 - row2)
    hi = min(row1, col1)
    total = 0.0
    for x in range(lo, hi + 1):
        lp = log_prob(x)
        if lp <= log_p_obs + _LOGP_TOL:
            total += math.exp(lp)
    return min(1.0, total)


def log2_odds_ratio(a: int, b: int, c: int, d: int) -> float:
    """log2 of the odds ratio (a*d)/(b*c), with the Haldane-Anscombe +0.5
    correction so the result is always finite.

    Positive => the two genes tend to co-occur; negative => mutual exclusivity.
    """
    aa, bb, cc, dd = a + 0.5, b + 0.5, c + 0.5, d + 0.5
    return math.log2((aa * dd) / (bb * cc))


def benjamini_hochberg(pvalues: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg FDR q-values, in the input order.

    Uses the standard step-up procedure with monotonicity enforced
    (q-values are non-decreasing in p-value rank).
    """
    n = len(pvalues)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: pvalues[i])
    q = [0.0] * n
    running_min = 1.0
    # Walk from the largest p-value down, enforcing monotonicity.
    for rank in range(n - 1, -1, -1):
        i = order[rank]
        scaled = pvalues[i] * n / (rank + 1)
        running_min = min(running_min, scaled)
        q[i] = min(1.0, running_min)
    return q


# ---------------------------------------------------------------------------
# Stratified 2x2xK association (confounder adjustment)
# ---------------------------------------------------------------------------

Table = tuple[int, int, int, int]  # (a, b, c, d) = [[a, b], [c, d]]

# Mantel-Fleiss: the CMH chi-square approximation is trusted when the expected
# top-left total sits at least this far from both of its attainable bounds.
MANTEL_FLEISS_MIN = 5.0


def _clean_tables(tables: Sequence[Table]) -> list[tuple[int, int, int, int]]:
    """Validate counts and drop strata with fewer than two observations."""
    out = []
    for t in tables:
        a, b, c, d = (int(x) for x in t)
        if min(a, b, c, d) < 0:
            raise ValueError("Contingency counts must be non-negative.")
        if a + b + c + d >= 2:
            out.append((a, b, c, d))
    return out


def _stratum_moments(a: int, b: int, c: int, d: int) -> tuple[float, float, int, int]:
    """Mean and variance of the top-left cell given the margins, and its bounds."""
    n = a + b + c + d
    row1, row2, col1, col2 = a + b, c + d, a + c, b + d
    mean = row1 * col1 / n
    var = row1 * row2 * col1 * col2 / (n * n * (n - 1)) if n > 1 else 0.0
    return mean, var, max(0, col1 - row2), min(row1, col1)


def mantel_haenszel_odds_ratio(tables: Sequence[Table]) -> float | None:
    """Mantel-Haenszel common odds ratio ``sum(a*d/n) / sum(b*c/n)``.

    ``None`` when both sums are zero (no stratum carries information);
    ``math.inf`` when only the denominator is zero.
    """
    num = den = 0.0
    for a, b, c, d in _clean_tables(tables):
        n = a + b + c + d
        num += a * d / n
        den += b * c / n
    if den == 0.0:
        return None if num == 0.0 else math.inf
    return num / den


def cmh_test(tables: Sequence[Table], correct: bool = True) -> dict:
    """Cochran-Mantel-Haenszel test of conditional independence in 2x2xK tables.

    ``X2 = (|sum(a) - sum(E[a])| - 0.5)^2 / sum(Var[a])`` on 1 df, the 0.5
    continuity correction applied only when the deviation is at least 0.5 (as R
    does). Returns ``{"chi_square", "p_value", "observed", "expected", "variance"}``;
    ``p_value`` is 1.0 when no stratum has any variance.
    """
    cleaned = _clean_tables(tables)
    observed = float(sum(t[0] for t in cleaned))
    expected = variance = 0.0
    for t in cleaned:
        mean, var, _, _ = _stratum_moments(*t)
        expected += mean
        variance += var
    if variance <= 0.0:
        return {
            "chi_square": 0.0,
            "p_value": 1.0,
            "observed": observed,
            "expected": expected,
            "variance": 0.0,
        }
    delta = abs(observed - expected)
    yates = 0.5 if correct and delta >= 0.5 else 0.0
    chi = (delta - yates) ** 2 / variance
    return {
        "chi_square": chi,
        "p_value": chi_square_sf(chi, 1),
        "observed": observed,
        "expected": expected,
        "variance": variance,
    }


def mantel_fleiss_criterion(tables: Sequence[Table]) -> float:
    """``min(sum(E - L), sum(U - E))`` over strata: distance of the expected
    top-left total from its attainable bounds. Small values mean the CMH
    chi-square approximation is unreliable."""
    low = high = 0.0
    for t in _clean_tables(tables):
        mean, _, lo, hi = _stratum_moments(*t)
        low += mean - lo
        high += hi - mean
    return min(low, high)


# Probabilities below this fraction of the mode are dropped from the convolution:
# far past double-precision relevance for any p-value this module reports.
_TRIM = 1e-280


def _trim(lo: int, pmf: list[float]) -> tuple[int, list[float]]:
    """Drop negligible values from both ends of a scaled distribution."""
    start, end = 0, len(pmf)
    while start < end - 1 and pmf[start] < _TRIM:
        start += 1
    while end - 1 > start and pmf[end - 1] < _TRIM:
        end -= 1
    return lo + start, pmf[start:end]


def _stratum_distribution(a: int, b: int, c: int, d: int) -> tuple[int, list[float]]:
    """Hypergeometric distribution of the top-left cell, scaled so its maximum is 1."""
    row1, row2, col1 = a + b, c + d, a + c
    lo, hi = max(0, col1 - row2), min(row1, col1)
    logs = [_log_comb(row1, x) + _log_comb(row2, col1 - x) for x in range(lo, hi + 1)]
    top = max(logs)
    return lo, [math.exp(v - top) for v in logs]


def exact_conditional_test(tables: Sequence[Table]) -> dict:
    """Exact conditional test of independence in 2x2xK tables, two-sided.

    The statistic ``S = sum(a_k)`` has, given every stratum's margins, the
    distribution of a sum of independent hypergeometrics; it is built by
    convolution and the p-value sums the probabilities of every value no more
    likely than the observed one (relative tolerance 1e-7), exactly as
    ``fisher.test`` and ``mantelhaen.test(exact = TRUE)`` do. With a single
    stratum this is Fisher's exact test.

    Cost grows with the product of the strata supports, so it is meant for
    sparse tables; ``stratified_association_test`` routes large ones to CMH.
    Returns ``{"statistic": S, "p_value": p, "expected": E[S]}``.
    """
    cleaned = _clean_tables(tables)
    observed = sum(t[0] for t in cleaned)
    expected = sum(_stratum_moments(*t)[0] for t in cleaned)
    offset, dist = 0, [1.0]
    for t in cleaned:
        lo, pmf = _stratum_distribution(*t)
        lo, pmf = _trim(lo, pmf)
        if len(pmf) == 1:
            offset += lo
            continue
        out = [0.0] * (len(dist) + len(pmf) - 1)
        for i, p_i in enumerate(dist):
            if p_i == 0.0:
                continue
            for j, p_j in enumerate(pmf):
                out[i + j] += p_i * p_j
        top = max(out)
        shift, dist = _trim(0, [v / top for v in out])
        offset += lo + shift
    index = observed - offset
    total = math.fsum(dist)
    if not 0 <= index < len(dist):
        # The observed total fell in a tail trimmed as negligible: every value
        # there is below _TRIM of the mode, so the two-sided p is too.
        return {"statistic": observed, "p_value": 0.0, "expected": expected}
    threshold = dist[index] * (1.0 + 1e-7)
    tail = math.fsum(v for v in dist if v <= threshold)
    return {
        "statistic": observed,
        "p_value": min(1.0, tail / total),
        "expected": expected,
    }


def stratified_association_test(
    tables: Sequence[Table], min_mantel_fleiss: float = MANTEL_FLEISS_MIN
) -> dict:
    """Test one 2x2 association across strata, adjusting for the stratifier.

    Uses the exact conditional test when the tables are sparse (Mantel-Fleiss
    criterion below ``min_mantel_fleiss``) or there is a single stratum, and the
    continuity-corrected CMH chi-square otherwise -- the regime where the
    approximation is sound and exact convolution gets expensive. Always reports
    the Mantel-Haenszel common odds ratio (as log2, Haldane-corrected per stratum
    when a sum is zero so it stays finite) and which test ran.
    """
    cleaned = _clean_tables(tables)
    informative = [t for t in cleaned if _stratum_moments(*t)[1] > 0.0]
    result: dict = {
        "method": None,
        "p_value": 1.0,
        "chi_square": None,
        "observed": float(sum(t[0] for t in cleaned)),
        "expected": sum(_stratum_moments(*t)[0] for t in cleaned),
        "n_strata": len(cleaned),
        "n_informative_strata": len(informative),
        "mantel_fleiss": None,
        "mh_odds_ratio": mantel_haenszel_odds_ratio(cleaned),
        "log2_mh_odds_ratio": None,
    }
    if informative:
        result["mantel_fleiss"] = mantel_fleiss_criterion(informative)
        if len(informative) == 1 or result["mantel_fleiss"] < min_mantel_fleiss:
            exact = exact_conditional_test(informative)
            result["method"] = "exact_conditional"
            result["p_value"] = exact["p_value"]
        else:
            cmh = cmh_test(informative)
            result["method"] = "cmh_chi_square"
            result["p_value"] = cmh["p_value"]
            result["chi_square"] = cmh["chi_square"]
    # Finite log2 odds ratio for ranking and colouring: +0.5 to every cell of every
    # stratum when the plain MH ratio is 0 or infinite.
    ratio = result["mh_odds_ratio"]
    if ratio is None or ratio == 0.0 or math.isinf(ratio):
        corrected = [(a + 0.5, b + 0.5, c + 0.5, d + 0.5) for a, b, c, d in cleaned]
        num = sum(a * d / (a + b + c + d) for a, b, c, d in corrected)
        den = sum(b * c / (a + b + c + d) for a, b, c, d in corrected)
        result["log2_mh_odds_ratio"] = math.log2(num / den) if num and den else 0.0
    else:
        result["log2_mh_odds_ratio"] = math.log2(ratio)
    return result
