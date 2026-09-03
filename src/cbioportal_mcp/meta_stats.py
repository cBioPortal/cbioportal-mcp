"""Meta-analysis statistics for pooling one proportion across independent studies.

Pure Python on the standard library, like ``survival_stats`` and
``cooccurrence_stats``: nothing here may import scipy or numpy. Everything is
closed-form -- no iteration to convergence -- so a call is cheap and
deterministic inside a tool invocation.

The toolkit is the standard one for proportions:

- :func:`wilson_interval` -- per-study confidence interval (Wilson score, which
  behaves at 0% and 100% where the Wald interval collapses).
- :func:`pooled_proportion` -- DerSimonian-Laird random-effects pooling on the
  logit scale, alongside the fixed-effect (inverse-variance) estimate,
  Cochran's Q, tau-squared and I-squared. This is what R's
  ``meta::metaprop(sm = "PLOGIT", method.tau = "DL")`` and
  ``metafor::rma(measure = "PLO", method = "DL")`` compute.
- :func:`homogeneity_test` -- "do the studies differ?": a k x 2 chi-square test
  of homogeneity, or Fisher's exact test for two small studies.

Studies are passed as ``(altered, profiled)`` integer pairs. Results are in
natural units (proportions in [0, 1], I-squared as a fraction); callers format
percentages.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from cbioportal_mcp.cooccurrence_stats import fisher_exact_two_sided
from cbioportal_mcp.survival_stats import chi_square_sf, normal_ppf

Study = tuple[int, int]  # (altered, profiled)


def _z(conf_level: float) -> float:
    if not 0.0 < conf_level < 1.0:
        raise ValueError("conf_level must be strictly between 0 and 1")
    return normal_ppf(1.0 - (1.0 - conf_level) / 2.0)


def _validate_study(altered: int, profiled: int) -> tuple[int, int]:
    try:
        a, n = int(altered), int(profiled)
    except (TypeError, ValueError) as e:
        raise ValueError(f"study counts must be integers, got ({altered!r}, {profiled!r})") from e
    if n <= 0:
        raise ValueError(f"profiled count must be > 0, got {n}")
    if not 0 <= a <= n:
        raise ValueError(f"altered count must be within [0, profiled], got ({a}, {n})")
    return a, n


def _validate_studies(studies: Sequence[Study]) -> list[tuple[int, int]]:
    cleaned = [_validate_study(a, n) for a, n in studies]
    if len(cleaned) < 2:
        raise ValueError("at least two studies are required")
    return cleaned


def logit(p: float) -> float:
    """log(p / (1 - p)) for 0 < p < 1."""
    if not 0.0 < p < 1.0:
        raise ValueError("logit requires 0 < p < 1")
    return math.log(p / (1.0 - p))


def expit(y: float) -> float:
    """Inverse logit, safe against overflow for large |y|."""
    if y >= 0:
        return 1.0 / (1.0 + math.exp(-y))
    e = math.exp(y)
    return e / (1.0 + e)


# ---------------------------------------------------------------------------
# Per-study interval
# ---------------------------------------------------------------------------


def wilson_interval(altered: int, profiled: int, conf_level: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Centre ``(p + z^2/2n) / (1 + z^2/n)``, half-width
    ``z * sqrt(p(1-p)/n + z^2/4n^2) / (1 + z^2/n)``. Never the Wald interval:
    at 0 or n altered that one has zero width, which is wrong.
    """
    a, n = _validate_study(altered, profiled)
    z = _z(conf_level)
    p = a / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


# ---------------------------------------------------------------------------
# DerSimonian-Laird pooling on the logit scale
# ---------------------------------------------------------------------------


def _logit_effect(a: int, n: int) -> tuple[float, float]:
    """Logit proportion and its (delta-method) variance for one study.

    A study at 0 or n altered has an infinite logit, so it gets the usual
    continuity correction: half an event added to each cell (``a + 0.5`` out of
    ``n + 1``). Every other study uses its raw counts.
    """
    if a == 0 or a == n:
        a2, n2 = a + 0.5, n + 1.0
    else:
        a2, n2 = float(a), float(n)
    y = math.log(a2 / (n2 - a2))
    v = 1.0 / a2 + 1.0 / (n2 - a2)
    return y, v


def heterogeneity(q: float, df: int) -> tuple[float, float]:
    """``(I-squared as a fraction in [0, 1], p-value of Cochran's Q)``."""
    if df < 1:
        raise ValueError("df must be >= 1")
    if q < 0.0:
        raise ValueError("Q cannot be negative")
    i2 = max(0.0, (q - df) / q) if q > 0.0 else 0.0
    return i2, chi_square_sf(q, df)


def pooled_proportion(studies: Sequence[Study], conf_level: float = 0.95) -> dict:
    """Pool ``(altered, profiled)`` pairs into one proportion, DerSimonian-Laird.

    On the logit scale, with ``y_i`` / ``v_i`` from :func:`_logit_effect`:

    - fixed effect: ``w_i = 1/v_i``, ``y_FE = sum(w_i y_i) / sum(w_i)``,
      ``SE = sqrt(1 / sum(w_i))``
    - ``Q = sum(w_i (y_i - y_FE)^2)``, ``df = k - 1``,
      ``C = sum(w_i) - sum(w_i^2) / sum(w_i)``, ``tau2 = max(0, (Q - df) / C)``
    - random effects: ``w*_i = 1 / (v_i + tau2)``, ``y_RE = sum(w*_i y_i) / sum(w*_i)``,
      ``SE = sqrt(1 / sum(w*_i))``

    Intervals are back-transformed with :func:`expit`. Requires ``k >= 2`` and
    every ``profiled > 0`` (raises ``ValueError`` otherwise). Returns::

        {
          "k": k,
          "fixed":  {"proportion": p, "ci": (lo, hi), "weights": [fractions summing to 1]},
          "random": {"proportion": p, "ci": (lo, hi), "weights": [...]},
          "q": Q, "df": df, "tau2": tau2, "i2": I2 fraction, "p_heterogeneity": p,
        }
    """
    cleaned = _validate_studies(studies)
    z = _z(conf_level)
    k = len(cleaned)

    effects = [_logit_effect(a, n) for a, n in cleaned]
    ys = [y for y, _ in effects]
    vs = [v for _, v in effects]

    w = [1.0 / v for v in vs]
    sum_w = sum(w)
    y_fe = sum(wi * yi for wi, yi in zip(w, ys, strict=True)) / sum_w
    se_fe = math.sqrt(1.0 / sum_w)

    q = sum(wi * (yi - y_fe) ** 2 for wi, yi in zip(w, ys, strict=True))
    df = k - 1
    c = sum_w - sum(wi * wi for wi in w) / sum_w
    tau2 = max(0.0, (q - df) / c) if c > 0.0 else 0.0

    w_re = [1.0 / (v + tau2) for v in vs]
    sum_w_re = sum(w_re)
    y_re = sum(wi * yi for wi, yi in zip(w_re, ys, strict=True)) / sum_w_re
    se_re = math.sqrt(1.0 / sum_w_re)

    i2, p_het = heterogeneity(q, df)

    return {
        "k": k,
        "fixed": {
            "proportion": expit(y_fe),
            "ci": (expit(y_fe - z * se_fe), expit(y_fe + z * se_fe)),
            "weights": [wi / sum_w for wi in w],
        },
        "random": {
            "proportion": expit(y_re),
            "ci": (expit(y_re - z * se_re), expit(y_re + z * se_re)),
            "weights": [wi / sum_w_re for wi in w_re],
        },
        "q": q,
        "df": df,
        "tau2": tau2,
        "i2": i2,
        "p_heterogeneity": p_het,
    }


# ---------------------------------------------------------------------------
# Do the studies differ?
# ---------------------------------------------------------------------------


def homogeneity_test(studies: Sequence[Study], min_expected: float = 5.0) -> dict:
    """Test that every study shares one underlying proportion.

    A k x 2 (study x altered/not) Pearson chi-square test against the crude
    pooled proportion, ``df = k - 1``. For exactly two studies where any expected
    cell is below ``min_expected`` the chi-square approximation is poor, so
    Fisher's exact test (two-sided) is used instead -- the same routine the
    co-occurrence app runs. Returns::

        {"test": "chi_square_homogeneity" | "fisher_exact",
         "statistic": chi-square or None, "df": k-1 or None, "p_value": p,
         "min_expected_cell": smallest expected count, "k": k}
    """
    cleaned = _validate_studies(studies)
    k = len(cleaned)
    total_altered = sum(a for a, _ in cleaned)
    total_profiled = sum(n for _, n in cleaned)
    p = total_altered / total_profiled

    if total_altered == 0 or total_altered == total_profiled:
        # Every study sits at exactly 0% or 100%: nothing to distinguish.
        return {
            "test": "chi_square_homogeneity",
            "statistic": 0.0,
            "df": k - 1,
            "p_value": 1.0,
            "min_expected_cell": 0.0,
            "k": k,
        }

    expected = []
    for _, n in cleaned:
        expected.append(n * p)
        expected.append(n * (1.0 - p))
    min_cell = min(expected)

    if k == 2 and min_cell < min_expected:
        (a1, n1), (a2, n2) = cleaned
        return {
            "test": "fisher_exact",
            "statistic": None,
            "df": None,
            "p_value": fisher_exact_two_sided(a1, n1 - a1, a2, n2 - a2),
            "min_expected_cell": min_cell,
            "k": k,
        }

    chi = 0.0
    for a, n in cleaned:
        e_alt = n * p
        e_not = n * (1.0 - p)
        chi += (a - e_alt) ** 2 / e_alt + ((n - a) - e_not) ** 2 / e_not
    return {
        "test": "chi_square_homogeneity",
        "statistic": chi,
        "df": k - 1,
        "p_value": chi_square_sf(chi, k - 1),
        "min_expected_cell": min_cell,
        "k": k,
    }
