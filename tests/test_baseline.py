import pytest

from fareloot.baseline import mad, median, percentile_rank, robust_z


def test_median_odd():
    assert median([3, 1, 2]) == 2


def test_median_even():
    assert median([1, 2, 3, 4]) == 2.5


def test_median_empty_raises():
    with pytest.raises(ValueError):
        median([])


def test_mad_basic():
    assert mad([1, 1, 1, 1]) == 0


def test_robust_z_none_below_min_obs():
    assert robust_z(100, [100, 200, 300]) is None


def test_robust_z_flags_a_crash():
    history = [20000, 21000, 19500, 20500, 20200, 19800]
    z = robust_z(6500, history)
    assert z is not None
    assert z < -3


def test_robust_z_zero_spread_history_does_not_explode():
    # Every observation identical: still must not divide by zero.
    z = robust_z(500, [1000, 1000, 1000, 1000])
    assert z is not None
    assert z < 0


def test_percentile_rank_cheapest_is_zero():
    assert percentile_rank(1, [5, 10, 15, 20]) == 0.0


def test_percentile_rank_priciest_is_full():
    assert percentile_rank(100, [5, 10, 15, 20]) == 1.0
