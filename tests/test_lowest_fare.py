"""Lowest-fare rule: for destinations flown often, alert when a round trip is the
cheapest seen in the window, with looser floors, but still only with a healthy
return leg and only once there is enough history for "lowest" to mean anything.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from fareloot.config import LowestFareRule, OriginModel, ReachModel, Watchlist
from fareloot.models import Quote, Tier
from fareloot.providers.mock import MockProvider
from fareloot.store import Store
from fareloot.watcher import poll, record_alerts, score_quotes

NOW = datetime.now(UTC)
DEPART = date.today() + timedelta(days=40)


def _wl() -> Watchlist:
    return Watchlist(
        origins=[
            OriginModel(
                iata="COK", name="Kochi", reach=ReachModel(mode="local", hours=1, cost_inr=200)
            )
        ],
        destinations=["DXB", "BKK"],
        lowest_fare_rules=[LowestFareRule(destinations=["DXB", "AUH", "SHJ", "RKT"])],
    )


def _rt(
    dest: str, price: int, *, days_ago: float = 0, depart: date = DEPART, length: int = 7
) -> Quote:
    return Quote(
        origin="COK",
        destination=dest,
        depart_date=depart,
        return_date=depart + timedelta(days=length),
        price_inr=price,
        distance_km=2700,
        observed_at=NOW - timedelta(days=days_ago),
    )


def _ow(origin: str, dest: str, price: int, *, days_ago: float = 0, depart: date = DEPART) -> Quote:
    return Quote(
        origin=origin,
        destination=dest,
        depart_date=depart,
        price_inr=price,
        observed_at=NOW - timedelta(days=days_ago),
    )


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "t.db") as s:
        yield s


def _history(
    store: Store, dest: str = "DXB", *, span_days: float = 10, n: int = 25, low: int = 19_000
):
    """n round-trip prices around Rs 21,000 spread over span_days, lowest = low,
    plus a reverse one-way baseline so the return leg can be judged."""
    rows = []
    for i in range(n):
        price = low if i == 0 else 20_000 + (i * 137) % 2_000
        depart = DEPART + timedelta(days=i % 5)
        rows.append(_rt(dest, price, days_ago=0.5 + span_days * i / (n - 1), depart=depart))
    rows += [_ow(dest, "COK", 10_000 + 50 * i, days_ago=1 + i) for i in range(10)]
    store.record(rows)


def _score(store, wl, quote, reverse_price=10_200):
    reverse = (
        {
            ("COK", quote.destination): [
                _ow(quote.destination, "COK", reverse_price, depart=quote.return_date)
            ]
        }
        if reverse_price
        else {}
    )
    deals = score_quotes(store, wl, [quote], reverse_quotes=reverse, as_of=NOW)
    assert len(deals) == 1
    return deals[0]


def test_new_30_day_low_with_healthy_return_alerts(store):
    _history(store)
    deal = _score(store, _wl(), _rt("DXB", 17_000))
    assert deal.lowest_in_window
    assert deal.tier.pushes and deal.tier.rank >= Tier.STEAL.rank
    assert "cheapest COK->DXB round trip in 30 days (previous low Rs 19,000)" in deal.reasons[0]


def test_global_rules_alone_would_not_have_alerted(store):
    # Rs 17,000 vs a ~Rs 21,000 norm is ~19% below: under the global 25% floor.
    _history(store)
    wl = _wl()
    wl.lowest_fare_rules = []
    deal = _score(store, wl, _rt("DXB", 17_000))
    assert not deal.lowest_in_window and not deal.tier.pushes


def test_not_below_previous_low(store):
    _history(store)
    assert not _score(store, _wl(), _rt("DXB", 19_000)).lowest_in_window


def test_expensive_return_still_blocks(store):
    _history(store)
    deal = _score(store, _wl(), _rt("DXB", 17_000), reverse_price=25_000)
    assert not deal.lowest_in_window and not deal.tier.pushes


def test_unchecked_return_blocks(store):
    _history(store)
    deal = _score(store, _wl(), _rt("DXB", 17_000), reverse_price=None)
    assert not deal.lowest_in_window


def test_dormant_until_history_spans_a_week(store):
    _history(store, span_days=3)
    assert not _score(store, _wl(), _rt("DXB", 17_000)).lowest_in_window


def test_dormant_with_too_few_prices(store):
    _history(store, n=10)
    assert not _score(store, _wl(), _rt("DXB", 17_000)).lowest_in_window


def test_tiny_new_low_below_floor_is_ignored(store):
    # Below the previous low, but only ~Rs 1,100 under the typical price.
    _history(store, low=20_000)
    deal = _score(store, _wl(), _rt("DXB", 19_950))
    assert not deal.lowest_in_window


def test_destination_without_a_rule_is_unaffected(store):
    _history(store, dest="BKK")
    deal = _score(store, _wl(), _rt("BKK", 17_000))
    assert not deal.lowest_in_window and not deal.tier.pushes


def test_duplicate_destination_across_rules_rejected():
    with pytest.raises(ValueError, match="more than one"):
        Watchlist(
            origins=[
                OriginModel(iata="COK", name="Kochi", reach=ReachModel(hours=1, cost_inr=200))
            ],
            lowest_fare_rules=[
                LowestFareRule(destinations=["DXB"]),
                LowestFareRule(destinations=["dxb"]),
            ],
        )


def _fresh(q: Quote) -> Quote:
    return Quote(**{**q.__dict__, "observed_at": datetime.now(UTC)})


class _ScriptedProvider(MockProvider):
    """Returns given forward round trips for COK->DXB and a healthy reverse leg.

    Stamped at fetch time, like the real providers, so they are newer than the
    run start and never count as their own previous low.
    """

    def __init__(self, forward: list[int], first_day: int = 0):
        super().__init__()
        self.forward = forward
        self.first_day = first_day

    def cheap(self, origin, destination, depart, return_=None):
        if origin == "COK" and destination == "DXB" and return_:
            return [
                _fresh(_rt("DXB", p, depart=DEPART + timedelta(days=self.first_day + i)))
                for i, p in enumerate(self.forward)
            ]
        if origin == "DXB" and destination == "COK" and not return_:
            return [_fresh(_ow("DXB", "COK", 10_200, depart=DEPART + timedelta(days=7)))]
        return []

    def city_directions(self, origin):
        return []

    def latest(self, origin, *, one_way=False, trip_duration=None):
        return []


def _wl_dxb_only() -> Watchlist:
    wl = _wl()
    wl.destinations = ["DXB"]
    return wl


def test_one_alert_per_route_per_run_and_it_is_the_cheapest(store):
    _history(store)
    result = poll(
        _ScriptedProvider([17_000, 16_500, 16_800]), store, _wl_dxb_only(), include_discovery=False
    )
    lowest = [d for d in result.to_alert if d.lowest_in_window]
    assert len(lowest) == 1
    assert lowest[0].quote.price_inr == 16_500


def test_route_cooldown_needs_a_real_further_drop(store):
    _history(store)
    wl = _wl_dxb_only()
    first = poll(_ScriptedProvider([16_500]), store, wl, include_discovery=False)
    record_alerts(store, first.to_alert)

    # A new low on another date, but only 3% lower: suppressed for the route.
    nudge = poll(_ScriptedProvider([16_000], first_day=3), store, wl, include_discovery=False)
    assert not [d for d in nudge.to_alert if d.lowest_in_window]

    # 15% lower than what was alerted: genuinely new information.
    drop = poll(_ScriptedProvider([14_000], first_day=4), store, wl, include_discovery=False)
    assert [d.quote.price_inr for d in drop.to_alert if d.lowest_in_window] == [14_000]
