"""Descriptive statistics, histogram binning and quantile splits.

Pure standard library, like the other ``*_stats`` modules: the tools compute every
summary a chart or answer quotes (mean, median, quartiles, bin counts, quantile
cut-offs) here, from the actual values, so a model never has to estimate one.

- :func:`quantile` -- R's default (type 7) linear-interpolation quantile.
- :func:`describe` -- n, mean, sd, min, quartiles, max.
- :func:`histogram` -- bin edges and counts (numpy semantics: half-open bins, the
  last one closed).
- :func:`quantile_split` -- assign ids to quantile groups (median / tertiles /
  quartiles / top-vs-bottom quartile / top quartile vs rest), reporting the exact
  cut-offs and how ties were placed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

MAX_BINS = 100
DEFAULT_MIN_BINS = 5
DEFAULT_MAX_BINS = 60

SPLITS = {
    "median": "low (<= median) vs high (> median)",
    "tertiles": "three groups at the 1/3 and 2/3 quantiles",
    "quartiles": "four groups at the quartiles",
    "top_vs_bottom_quartile": (
        "top quartile (> Q3) vs bottom quartile (<= Q1); middle half excluded"
    ),
    "top_quartile_vs_rest": "top quartile (> Q3) vs the rest (<= Q3)",
}


def _finite(values: Sequence[float]) -> list[float]:
    out = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Type-7 quantile of already-sorted values (``quantile(x, q)`` in R)."""
    if not sorted_values:
        raise ValueError("quantile of an empty sequence")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be within [0, 1]")
    h = (len(sorted_values) - 1) * q
    lo = math.floor(h)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (h - lo) * (sorted_values[hi] - sorted_values[lo])


def describe(values: Sequence[float]) -> dict:
    """n, mean, sample sd, min, q1, median, q3, max over the finite values."""
    xs = sorted(_finite(values))
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None, "min": None, "q1": None,
                "median": None, "q3": None, "max": None}  # fmt: skip
    mean = math.fsum(xs) / n
    sd = math.sqrt(math.fsum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else None
    return {
        "n": n,
        "mean": mean,
        "sd": sd,
        "min": xs[0],
        "q1": quantile(xs, 0.25),
        "median": quantile(xs, 0.5),
        "q3": quantile(xs, 0.75),
        "max": xs[-1],
    }


def _auto_bin_count(xs: Sequence[float]) -> int:
    """Freedman-Diaconis bin count, Sturges when the IQR is zero, clamped."""
    n = len(xs)
    lo, hi = xs[0], xs[-1]
    if n < 2 or hi == lo:
        return 1
    iqr = quantile(xs, 0.75) - quantile(xs, 0.25)
    if iqr > 0:
        width = 2.0 * iqr / n ** (1.0 / 3.0)
        count = math.ceil((hi - lo) / width)
    else:
        count = math.ceil(math.log2(n)) + 1
    return max(DEFAULT_MIN_BINS, min(DEFAULT_MAX_BINS, count))


def histogram(
    values: Sequence[float],
    bins: int | None = None,
    value_range: tuple[float, float] | None = None,
) -> dict:
    """Equal-width histogram.

    Bins are ``[e_i, e_{i+1})`` except the last, which is closed, so the maximum
    lands in it (numpy semantics). Values outside ``value_range`` are counted in
    ``n_outside`` and not binned. Returns ``{"edges", "counts", "n_binned",
    "n_outside", "bin_width"}``.
    """
    xs = sorted(_finite(values))
    if not xs:
        raise ValueError("histogram needs at least one finite value")
    if value_range is not None:
        lo, hi = float(value_range[0]), float(value_range[1])
        if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
            raise ValueError("value_range must be (low, high) with low < high")
    else:
        lo, hi = xs[0], xs[-1]
        if hi == lo:
            lo, hi = lo - 0.5, hi + 0.5
    if bins is None:
        inside = [x for x in xs if lo <= x <= hi]
        bins = _auto_bin_count(inside) if inside else DEFAULT_MIN_BINS
    bins = int(bins)
    if not 1 <= bins <= MAX_BINS:
        raise ValueError(f"bins must be between 1 and {MAX_BINS}")
    width = (hi - lo) / bins
    edges = [lo + i * width for i in range(bins)] + [hi]
    counts = [0] * bins
    outside = 0
    for x in xs:
        if x < lo or x > hi:
            outside += 1
            continue
        i = bins - 1 if x == hi else min(bins - 1, int((x - lo) / width))
        counts[i] += 1
    return {
        "edges": edges,
        "counts": counts,
        "n_binned": sum(counts),
        "n_outside": outside,
        "bin_width": width,
    }


def quantile_split(values: Mapping[str, float], split: str) -> dict:
    """Assign ids to quantile groups of their values.

    Cut-offs are type-7 quantiles of the values; an id goes to the lowest group
    whose upper cut-off its value does not exceed (``<=``), so ties at a cut-off
    fall into the lower group and group sizes can be uneven -- ``groups`` reports
    the sizes actually produced. Returns ``{"split", "description", "cutoffs":
    {name: value}, "groups": [{"name", "range", "ids"}], "n_excluded"}``;
    ``n_excluded`` counts ids a two-sided split (top vs bottom quartile) leaves out.
    """
    if split not in SPLITS:
        raise ValueError(f"split must be one of {', '.join(SPLITS)}; got {split!r}")
    clean = {k: float(v) for k, v in values.items() if v is not None and math.isfinite(float(v))}
    if len(clean) < 4:
        raise ValueError("A quantile split needs at least four values.")
    xs = sorted(clean.values())
    if split == "median":
        cuts = {"median": quantile(xs, 0.5)}
    elif split == "tertiles":
        cuts = {"t1": quantile(xs, 1 / 3), "t2": quantile(xs, 2 / 3)}
    else:
        cuts = {"q1": quantile(xs, 0.25), "q2": quantile(xs, 0.5), "q3": quantile(xs, 0.75)}

    def band(value: float, bounds: list[float]) -> int:
        for i, b in enumerate(bounds):
            if value <= b:
                return i
        return len(bounds)

    if split in ("median", "tertiles", "quartiles"):
        bounds = list(cuts.values())
        names = {
            "median": ["low", "high"],
            "tertiles": ["low tertile", "middle tertile", "high tertile"],
            "quartiles": ["Q1 (lowest)", "Q2", "Q3", "Q4 (highest)"],
        }[split]
        buckets: list[list[str]] = [[] for _ in names]
        for k, v in clean.items():
            buckets[band(v, bounds)].append(k)
        edges = [None, *bounds, None]
        groups = [
            {"name": names[i], "range": [edges[i], edges[i + 1]], "ids": sorted(buckets[i])}
            for i in range(len(names))
        ]
        excluded = 0
    elif split == "top_vs_bottom_quartile":
        high = sorted(k for k, v in clean.items() if v > cuts["q3"])
        low = sorted(k for k, v in clean.items() if v <= cuts["q1"])
        groups = [
            {"name": "top quartile", "range": [cuts["q3"], None], "ids": high},
            {"name": "bottom quartile", "range": [None, cuts["q1"]], "ids": low},
        ]
        excluded = len(clean) - len(high) - len(low)
    else:  # top_quartile_vs_rest
        high = sorted(k for k, v in clean.items() if v > cuts["q3"])
        rest = sorted(k for k, v in clean.items() if v <= cuts["q3"])
        groups = [
            {"name": "top quartile", "range": [cuts["q3"], None], "ids": high},
            {"name": "rest (<= Q3)", "range": [None, cuts["q3"]], "ids": rest},
        ]
        excluded = 0
    return {
        "split": split,
        "description": SPLITS[split],
        "cutoffs": cuts,
        "groups": groups,
        "n_excluded": excluded,
    }
