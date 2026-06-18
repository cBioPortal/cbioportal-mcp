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
"""

from __future__ import annotations

import math
from typing import Sequence

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
