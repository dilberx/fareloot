import pytest

from fareloot.journey import ranking_cost_inr, reach_score, total_cost_inr
from fareloot.models import Reach

LOCAL = Reach(mode="local", hours=1, cost_inr=200)
FAR = Reach(mode="overnight_train", hours=14, cost_inr=1000)


def test_total_cost_adds_round_trip_reach():
    assert total_cost_inr(10_000, LOCAL) == 10_000 + 400


def test_ranking_cost_penalises_travel_time():
    cheap_but_far = ranking_cost_inr(9_000, FAR, train_hour_value_inr=80)
    pricier_but_local = ranking_cost_inr(9_400, LOCAL, train_hour_value_inr=80)
    # A ~Rs 400 saving should not survive a 28-hour round trip on rail.
    assert cheap_but_far > pricier_but_local


def test_reach_score_bounds():
    doorstep = Reach(mode="local", hours=0, cost_inr=0)
    assert reach_score(doorstep, max_reach_hours=14) == pytest.approx(1.0)
    assert reach_score(FAR, max_reach_hours=14) == pytest.approx(0.0)
    # A short local hop should still clearly beat a 14h overnight train.
    assert reach_score(LOCAL, max_reach_hours=14) > reach_score(FAR, max_reach_hours=14)
