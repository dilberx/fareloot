from datetime import UTC, date, datetime, timedelta

import pytest

from fareloot.analysis import analyse, private_report, public_report
from fareloot.config import LowestFareRule, OriginModel, ReachModel, Watchlist
from fareloot.models import Quote
from fareloot.store import Store

NOW = datetime(2026, 10, 18, tzinfo=UTC)
D = date(2026, 11, 10)


def _wl():
    return Watchlist(
        origins=[
            OriginModel(iata=c, name=c, reach=ReachModel(hours=1, cost_inr=100))
            for c in ("COK", "BLR")
        ],
        destinations=["DXB", "SIN", "GYD"],
        lowest_fare_rules=[LowestFareRule(destinations=["DXB"])],
    )


def _rt(o, d, length, days_ago, price=20000):
    return Quote(
        origin=o,
        destination=d,
        depart_date=D + timedelta(days=days_ago % 5),
        return_date=D + timedelta(days=days_ago % 5 + length),
        price_inr=price + days_ago,
        observed_at=NOW - timedelta(days=days_ago),
    )


@pytest.fixture
def conn(tmp_path):
    with Store(tmp_path / "t.db") as s:
        s.record([_rt("COK", "DXB", 7, i) for i in range(22)])  # busy, 21-day span
        s.record([_rt("BLR", "SIN", 8, i) for i in range(2)])  # thin
        s.record([_rt("COK", "DXB", 1, i) for i in range(5)])  # out of band: ignored
        s.record([_rt("COK", "MLE", 7, i) for i in range(3)])  # discovery-only
        s.record(
            [
                Quote(
                    origin="DXB",
                    destination="COK",
                    depart_date=D,
                    price_inr=9000 + i,
                    observed_at=NOW - timedelta(days=i),
                )
                for i in range(4)
            ]
        )
        yield s.conn


def test_counts_and_yield(conn):
    a = analyse(conn, _wl())
    by = {y.code: y for y in a.wishlist}
    assert a.round_trips == 27  # 22 + 2 + 3; the 1-day trips are out of band
    assert by["DXB"].round_trips == 22 and by["DXB"].one_ways_back == 4
    assert by["DXB"].scoreable_bands == 1 and by["DXB"].confident_bands == 1
    assert by["DXB"].span_days == pytest.approx(21, abs=0.01)
    assert by["SIN"].scoreable_bands == 0
    assert by["GYD"].round_trips == 0
    assert a.discovery_only == [("MLE", 3)]
    assert a.per_origin["COK"][0] == 25 and a.per_origin["BLR"][0] == 2


def test_rule_readiness_needs_history_and_span(conn):
    a = analyse(conn, _wl())
    # DXB rule x 2 origins x 3 bands; only COK-DXB 7-10 has 20+ prices over 7+ days.
    assert (a.rule_routes_ready, a.rule_routes_total) == (1, 6)


def test_public_report_never_names_a_destination(conn):
    text = public_report(analyse(conn, _wl()))
    for code in ("DXB", "SIN", "GYD", "MLE"):
        assert code not in text
    assert "0: 1, 1-3: 1, 4-19: 0, 20+: 1" in text


def test_private_report_flags_dead_destinations(conn):
    text = private_report(analyse(conn, _wl()))
    assert "GYD: 0 | 0/0 | 0 | 0.0d  <- nothing yet" in text
    assert "MLE 3" in text
