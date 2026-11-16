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
        pct_below=(20000 - quote.price_inr) / 200,
        one_way_only=False,
        reasons=[],
    )


def test_one_alert_per_route_month_with_a_menu_of_other_trip_lengths():
    # Dubai is mid-distance: the menu groups are 3-4, 5-7, 8-10 and 11-14 nights.
    wl = _wl([("COK", "local", 1, 200)], ["DXB"])
    dec = [
        _deal(_q("COK", "DXB", 9000, date(2026, 12, 5), 7)),
        _deal(_q("COK", "DXB", 9000, date(2026, 12, 9), 5)),
        _deal(_q("COK", "DXB", 9900, date(2026, 12, 3), 4)),
        _deal(_q("COK", "DXB", 9700, date(2026, 12, 16), 3)),
        _deal(_q("COK", "DXB", 9400, date(2026, 12, 20), 9)),
        _deal(_q("COK", "DXB", 19500, date(2026, 12, 2), 12), tier=Tier.NONE),
        _deal(_q("COK", "DXB", 8800, date(2026, 12, 14), 18), tier=Tier.NONE),
    ]
    jan = [_deal(_q("COK", "DXB", 9900, date(2027, 1, 8), 7))]
    leads = pick_leads(dec + jan, wl)
    assert len(leads) == 2
    lead = next(d for d in leads if d.quote.depart_month == "2026-12")
    # Equal price: the shorter trip leads.
    assert lead.quote.trip_length == 5
    # One fare per other length group, the cheapest in it, shortest first. The
    # 7-night fare shares the lead's group; 18 nights is past every group.
    assert [(o.quote.trip_length, o.quote.price_inr) for o in lead.options] == [
        (3, 9700),
        (9, 9400),
        (12, 19500),
    ]

    text = render(lead)
    assert "Other trip lengths from Kochi in December:" in text
    assert "- 9 nights, Sun 20 Dec to Tue 29 Dec: Rs 9,400, 53% below usual" in text
    # A longer stay at a normal fare is offered, and labelled as no bargain.
    assert "- 12 nights, Wed 02 Dec to Mon 14 Dec: Rs 19,500, about the usual price" in text
    assert "not re-checked live" in text


def test_each_menu_line_links_to_its_own_dates_in_html():
    wl = _wl([("COK", "local", 1, 200)], ["DXB"])
    deals = [
        _deal(_q("COK", "DXB", 9000, date(2026, 12, 9), 5)),
        _deal(_q("COK", "DXB", 9400, date(2026, 12, 20), 9)),
    ]
    [lead] = pick_leads(deals, wl)
    html = render(lead, html=True)
    assert html.count("<a href=") == 3  # two booking links plus one menu line
    assert "2026-12-20" in html and ">Sun 20 Dec to Tue 29 Dec</a>" in html


def test_route_with_one_trip_length_still_offers_other_dates():
    wl = _wl([("COK", "local", 1, 200)], ["DXB"])
    deals = [
        _deal(_q("COK", "DXB", 9000, date(2026, 12, 9), 5)),
        _deal(_q("COK", "DXB", 9300, date(2026, 12, 12), 6)),
        _deal(_q("COK", "DXB", 9100, date(2026, 12, 15), 7)),
        _deal(_q("COK", "DXB", 9800, date(2026, 12, 18), 5)),
    ]
    [lead] = pick_leads(deals, wl)
    assert [o.quote.price_inr for o in lead.options] == [9300, 9100]


class _Live:
    """Live prices scripted by departure date; a missing date isn't listed."""

    def __init__(self, prices):
        self.prices, self.asked = prices, []

    def live_price(self, q):
        self.asked.append(q.depart_date)
        return self.prices.get(q.depart_date)


def _dubai_december(wl):
    deals = [
        _deal(_q("COK", "DXB", 8000, date(2026, 12, 5), 7)),
        _deal(_q("COK", "DXB", 9000, date(2026, 12, 9), 5)),
        _deal(_q("COK", "DXB", 9400, date(2026, 12, 20), 9)),
        _deal(_q("COK", "DXB", 9600, date(2026, 12, 22), 6)),
    ]
    return deals, wl


def test_stale_lead_hands_over_to_the_next_best_date(store):
    from fareloot.confirm import confirm_alerts

    deals, wl = _dubai_december(_wl([("COK", "local", 1, 200)], ["DXB"]))
    [lead] = pick_leads(deals, wl)
    assert lead.quote.price_inr == 8000
    assert [a.quote.price_inr for a in lead.alternates] == [9000, 9400]

    # The cheapest fare is a dead cache entry (live 14,000); the next one holds.
    live = _Live({date(2026, 12, 5): 14000, date(2026, 12, 9): 9100})
    send, stale = confirm_alerts([lead], live, store, monthly_cap=200, now=NOW)
    assert [d.quote.price_inr for d in send] == [9000] and send[0].live_price_inr == 9100
    assert [d.quote.price_inr for d in stale] == [8000]
    # The dead date isn't offered as an option on the alert that goes out.
    assert 8000 not in [o.quote.price_inr for o in send[0].options]
    assert store.get_meta("live_checks:2026-11") == "2"


def test_every_candidate_stale_sends_nothing_and_stops_at_the_fallback_cap(store):
    from fareloot.confirm import confirm_alerts

    deals, wl = _dubai_december(_wl([("COK", "local", 1, 200)], ["DXB"]))
    [lead] = pick_leads(deals, wl)
    live = _Live({d.quote.depart_date: 20000 for d in deals})
    send, stale = confirm_alerts([lead], live, store, monthly_cap=200, now=NOW)
    assert send == [] and len(stale) == 3  # the lead plus two alternates, not all four
    assert len(live.asked) == 3


def test_without_a_live_check_the_lead_goes_out_and_alternates_stay_put(store):
    from fareloot.confirm import confirm_alerts

    deals, wl = _dubai_december(_wl([("COK", "local", 1, 200)], ["DXB"]))
    [lead] = pick_leads(deals, wl)
    send, stale = confirm_alerts([lead], None, store, monthly_cap=200, now=NOW)
    assert send == [lead] and stale == []


def test_a_date_found_gone_neither_leads_nor_shows_as_an_option():
    deals, wl = _dubai_december(_wl([("COK", "local", 1, 200)], ["DXB"]))
    cooled = frozenset({("COK", "DXB", date(2026, 12, 5), date(2026, 12, 12))})
    [lead] = pick_leads(deals, wl, cooled)
    assert lead.quote.price_inr == 9000
    assert 8000 not in [o.quote.price_inr for o in lead.options]


def test_stale_fare_cools_only_its_own_date_not_the_route_month(store):
    # The bug: a fare found gone was recorded under the month key, so one dead
    # cache entry silenced every other date on its route for the whole cooldown,
    # then led again when it ran out.
    from fareloot.providers.mock import MockProvider
    from fareloot.watcher import poll, record_alerts, record_stale

    wl = _wl([("COK", "local", 1, 200)], ["GYD"])
    wl.search_horizon_months = 2
    today = date(2026, 11, 1)
    provider = MockProvider(today=today)
    for month in ("2026-11", "2026-12"):
        for a, b in (("COK", "GYD"), ("GYD", "COK")):
            store.record(provider.month_matrix(a, b, month))

    first = poll(provider, store, wl, today=today).to_alert
    assert first
    record_stale(store, first)
    second = poll(provider, store, wl, today=today).to_alert
    # Same route-months alert again, each led by a different date.
    assert {d.quote.depart_month for d in second} == {d.quote.depart_month for d in first}
    assert not {_key(d) for d in second} & {_key(d) for d in first}
    assert all(_key(f) not in {_key(o) for o in d.options} for d in second for f in first)

    # A delivered alert still cools the whole month.
    record_alerts(store, second)
    assert poll(provider, store, wl, today=today).to_alert == []


def _key(deal):
    return deal.quote.depart_date, deal.quote.return_date
