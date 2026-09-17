"""The two cases that matter most, per the product requirement:

1. A genuine round-trip crash must reach a pushable tier (STEAL or above).
2. A cheap outbound paired with a gouging return must NOT reach a pushable
   tier, no matter how deep the outbound discount looks.
"""

from datetime import UTC

from fareloot.models import Reach, Tier
from fareloot.scoring import Thresholds, score

REACH = Reach(mode="local", hours=1, cost_inr=200)
# 20 points: enough for scoring's confidence gate to reach full strength, which
# matches a realistically seeded route rather than a brand-new one.
HISTORY = [
    20000, 21000, 19500, 20500, 20200, 19800, 20100, 19700, 20300, 20050,
    20400, 19900, 20600, 19600, 20250, 19950, 20150, 19850, 20350, 20000,
]


def test_genuine_crash_pushes():
    result = score(
        price_inr=6500,
        route_history=HISTORY,
        reach=REACH,
        max_reach_hours=14,
        is_round_trip=True,
        return_z=-0.2,  # return leg is healthy
    )
    assert result.tier.pushes
    assert result.tier in (Tier.STEAL, Tier.LOOT, Tier.JACKPOT)


def test_expensive_return_leg_blocks_the_push():
    result = score(
        price_inr=6500,  # identical, deep "discount" on the outbound-facing total
        route_history=HISTORY,
        reach=REACH,
        max_reach_hours=14,
        is_round_trip=True,
        return_z=5.4,  # the trap: return market is badly elevated
    )
    assert not result.tier.pushes
    assert result.tier.rank <= Tier.STEAL.rank
    assert any("return" in r for r in result.reasons)


def test_one_way_only_never_pushes():
    result = score(
        price_inr=6500,
        route_history=HISTORY,
        reach=REACH,
        max_reach_hours=14,
        is_round_trip=False,
        return_z=None,
    )
    assert not result.tier.pushes
    assert result.one_way_only


def test_small_saving_is_filtered_by_absolute_floor():
    history = [1800, 1850, 1820, 1790, 1810, 1830, 1795, 1805, 1815, 1825]
    result = score(
        price_inr=1500,  # a clean ~17% drop, but trivial in rupees
        route_history=history,
        reach=REACH,
        max_reach_hours=14,
        is_round_trip=True,
        return_z=0.0,
        thresholds=Thresholds(min_saving_inr=3000, min_pct_below_baseline=25),
    )
    assert result.tier == Tier.NONE


def test_sparse_history_cannot_reach_jackpot():
    # Fewer than baseline.MIN_OBS points: robust_z returns None, so this must
    # fall back to the "not enough history" path rather than manufacturing a
    # score from noise.
    result = score(
        price_inr=100,
        route_history=[20000, 21000],
        reach=REACH,
        max_reach_hours=14,
        is_round_trip=True,
        return_z=0.0,
    )
    assert result.tier == Tier.NONE


def test_no_history_and_no_km_value_scores_zero():
    result = score(
        price_inr=6500,
        route_history=[],
        reach=REACH,
        max_reach_hours=14,
        is_round_trip=True,
        return_z=None,
    )
    assert result.hotness == 0
    assert result.tier == Tier.NONE


def test_farther_reach_scores_lower_than_local_for_identical_price():
    far_reach = Reach(mode="overnight_train", hours=14, cost_inr=1000)
    kwargs = dict(
        price_inr=6500,
        route_history=HISTORY,
        max_reach_hours=14,
        is_round_trip=True,
        return_z=0.0,
    )
    local = score(reach=REACH, **kwargs)
    far = score(reach=far_reach, **kwargs)
    assert far.hotness <= local.hotness


def test_expired_quote_never_pushes():
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    result = score(
        price_inr=6500,
        route_history=HISTORY,
        reach=REACH,
        max_reach_hours=14,
        is_round_trip=True,
        return_z=0.0,
        expires_at=now - timedelta(hours=1),
        now=now,
    )
    assert result.tier == Tier.NONE
