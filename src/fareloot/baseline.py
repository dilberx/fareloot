"""Robust price statistics for a single route.

Fare distributions are skewed and spiky: one festival-season observation can be
three times the usual price. Mean and standard deviation chase those outliers,
so everything here uses median and MAD instead.
"""

from __future__ import annotations

from collections.abc import Sequence

MAD_SCALE = 1.4826
MIN_OBS = 4


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def mad(values: Sequence[float], med: float | None = None) -> float:
    """Median absolute deviation."""
    if not values:
        raise ValueError("mad of empty sequence")
    centre = median(values) if med is None else med
    return median([abs(v - centre) for v in values])


def robust_z(price: float, values: Sequence[float], *, min_obs: int = MIN_OBS) -> float | None:
    """How many robust deviations below (negative) or above the norm.

    Returns None when there is too little history to say anything honest.
    """
    if len(values) < min_obs:
        return None
    centre = median(values)
    scale = MAD_SCALE * mad(values, centre)
    if scale <= 0:
        # Every observation is identical. Fall back to a relative scale so a
        # genuine drop still registers instead of dividing by zero.
        scale = max(centre * 0.05, 1.0)
    return (price - centre) / scale


def percentile_rank(price: float, values: Sequence[float]) -> float:
    """Fraction of observations strictly below `price`, in 0..1."""
    if not values:
        return 0.5
    below = sum(1 for v in values if v < price)
    return below / len(values)
