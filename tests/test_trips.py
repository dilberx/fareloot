"""A good fare is not always a good trip.

Scoring judges a fare against its own route. These pin the trip-level rules on
top: no overnight train for a short hop, no deal when another airport is
cheaper door to door, trip lengths that fit the distance, and one alert per
route-month that shows the other dates.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from fareloot.config import OriginModel, ReachModel, Watchlist
from fareloot.geo import COORDS, km
from fareloot.journey import trip_window, worth_the_detour
from fareloot.models import Deal, Origin, Quote, Reach, Tier
from fareloot.notify.message import render
from fareloot.places import PLACES
from fareloot.store import Store
from fareloot.watcher import _wishlist_slice, pick_leads, score_quotes

LOCAL = Reach(mode="local", hours=1, cost_inr=200)
OVERNIGHT = Reach(mode="overnight_train", hours=12, cost_inr=900)
NOW = datetime(2026, 11, 1, tzinfo=UTC)


def test_every_named_place_has_coordinates():
    assert set(PLACES) <= set(COORDS)


def test_distances_are_real():
    assert 2700 < km("COK", "DXB") < 2900
    assert 400 < km("BLR", "HYD") < 550
    assert km("COK", "XXX") is None


def test_overnight_train_only_for_flights_long_enough_to_be_worth_it():
    assert not worth_the_detour(OVERNIGHT, km("BLR", "HYD"))
    assert not worth_the_detour(OVERNIGHT, km("BLR", "GOI"))
    assert not worth_the_detour(OVERNIGHT, km("BLR", "DEL"))
    assert worth_the_detour(OVERNIGHT, km("BLR", "SIN"))
    assert worth_the_detour(OVERNIGHT, km("BLR", "YUL"))
    # The home airport and short trains are always fine; unknown can't be judged.
    assert worth_the_detour(LOCAL, km("COK", "TRV"))
    assert worth_the_detour(Reach(mode="train", hours=4.5, cost_inr=400), km("TRV", "CMB"))
    assert worth_the_detour(OVERNIGHT, None)


def test_trip_length_scales_with_distance():
    assert trip_window(km("COK", "GOI")) == (3, 6)
    assert trip_window(km("COK", "DXB")) == (3, 10)
    assert trip_window(km("COK", "YUL")) == (7, 14)
    assert trip_window(None) == (3, 10)


def _wl(origins, destinations):
    return Watchlist(
        origins=[
            OriginModel(iata=i, name=i, reach=ReachModel(mode=m, hours=h, cost_inr=c))
            for i, m, h, c in origins
        ],
        destinations=destinations,
        train_hour_value_inr=80,
    )


KOCHI_AND_BANGALORE = [("COK", "local", 1, 200), ("BLR", "overnight_train", 12, 900)]


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "t.db") as s:
        yield s


def _q(origin, dest, price, depart=date(2026, 12, 10), nights=7, observed=NOW):
    return Quote(
        origin=origin,
        destination=dest,
        depart_date=depart,
        return_date=depart + timedelta(days=nights),
        price_inr=price,
        observed_at=observed,
    )


def _history(store, origin, dest, price, n=20, nights=7):
    store.record(
        [
            _q(origin, dest, price + i * 50, date(2026, 12, 1) + timedelta(days=i), nights, NOW)
            for i in range(n)
        ]
    )


def test_short_hop_pairs_from_an_overnight_train_airport_are_never_priced(store):
    wl = _wl(KOCHI_AND_BANGALORE, ["HYD", "SIN"])
    pairs, _, _ = _wishlist_slice(store, wl, wl.origin_map(), None, 0)
    assert ("BLR", "HYD") not in pairs
    assert {("COK", "HYD"), ("COK", "SIN"), ("BLR", "SIN")} <= set(pairs)


def test_discovered_short_hop_from_bangalore_is_never_a_deal(store):
    wl = _wl(KOCHI_AND_BANGALORE, [])
    _history(store, "BLR", "HYD", 6000)
    deals = score_quotes(store, wl, [_q("BLR", "HYD", 2000)], as_of=NOW + timedelta(hours=1))
    assert deals == []


def test_fare_beaten_by_another_airports_usual_price_door_to_door_is_held(store):
    # Bangalore -> Singapore 35% below Bangalore's usual, but Kochi usually
    # costs less once the overnight train both ways is counted.
    wl = _wl(KOCHI_AND_BANGALORE, ["SIN"])
    _history(store, "BLR", "SIN", 20000)
    _history(store, "COK", "SIN", 14000)
    [deal] = score_quotes(store, wl, [_q("BLR", "SIN", 13000)], as_of=NOW + timedelta(hours=1))
    assert deal.tier == Tier.NONE
    assert "flying from Kochi usually costs less door to door" in deal.reasons[-1]


def test_fare_that_beats_every_airport_door_to_door_still_alerts(store):
    wl = _wl(KOCHI_AND_BANGALORE, ["SIN"])
    _history(store, "BLR", "SIN", 20000)
    _history(store, "COK", "SIN", 21000)
    [deal] = score_quotes(store, wl, [_q("BLR", "SIN", 9000)], as_of=NOW + timedelta(hours=1))
    assert deal.tier.pushes


def test_cheaper_fare_from_another_airport_this_run_holds_the_deal(store):
    wl = _wl(KOCHI_AND_BANGALORE, ["SIN"])
    _history(store, "BLR", "SIN", 20000)
    run = [_q("BLR", "SIN", 9000), _q("COK", "SIN", 9500, depart=date(2026, 12, 12))]
    deals = score_quotes(store, wl, run, as_of=NOW + timedelta(hours=1))
    blr = next(d for d in deals if d.quote.origin == "BLR")
    assert blr.tier == Tier.NONE
    assert "Kochi is cheaper door to door for nearly the same dates" in blr.reasons[-1]


def test_trip_too_long_for_the_distance_never_pushes(store):
    # The Montreal complaint: 15 nights is past the 7-14 a long haul is worth.
    wl = _wl([("COK", "local", 1, 200)], ["YUL"])
    _history(store, "COK", "YUL", 120000, nights=15)
    _history(store, "COK", "YUL", 120000, nights=10)
    as_of = NOW + timedelta(hours=1)
    [long] = score_quotes(store, wl, [_q("COK", "YUL", 60000, nights=15)], as_of=as_of)
    assert long.tier == Tier.NONE and "15 nights is outside 7-14" in long.reasons[-1]
    [ok] = score_quotes(store, wl, [_q("COK", "YUL", 60000, nights=10)], as_of=as_of)
    assert ok.tier.pushes


def _deal(quote, tier=Tier.STEAL, origin=None):
    origin = origin or Origin(iata=quote.origin, name=quote.origin, reach=LOCAL)
    return Deal(
        quote=quote,
        origin=origin,
        hotness=70,
        tier=tier,
        baseline_inr=20000,
        saving_inr=20000 - quote.price_inr,
        pct_below=40.0,
        one_way_only=False,
        reasons=[],
    )


def test_one_alert_per_route_month_with_the_other_dates_attached():
    wl = _wl([("COK", "local", 1, 200)], ["DXB"])
    dec = [
        _deal(_q("COK", "DXB", 9000, date(2026, 12, 5), 7)),
        _deal(_q("COK", "DXB", 9000, date(2026, 12, 9), 5)),
        _deal(_q("COK", "DXB", 9400, date(2026, 12, 20), 9)),
        _deal(_q("COK", "DXB", 8800, date(2026, 12, 14), 18), tier=Tier.NONE),
    ]
    jan = [_deal(_q("COK", "DXB", 9900, date(2027, 1, 8), 7))]
    leads = pick_leads(dec + jan, wl)
    assert len(leads) == 2
    lead = next(d for d in leads if d.quote.depart_month == "2026-12")
    # Equal price: the shorter trip leads.
    assert lead.quote.trip_length == 5
    # Options include a trip that can't lead (18 nights), cheapest first.
    assert [o.price_inr for o in lead.options] == [8800, 9000, 9400]

    text = render(lead)
    assert "Other dates from Kochi in December:" in text
    assert "- Mon 14 Dec to Fri 01 Jan, 18 nights: Rs 8,800" in text
