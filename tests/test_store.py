from datetime import UTC, date, timedelta

import pytest

from fareloot.models import Quote, Tier
from fareloot.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


def _quote(price=10_000, depart_offset=30, trip_length=10):
    depart = date.today() + timedelta(days=depart_offset)
    return Quote(
        origin="COK",
        destination="GYD",
        depart_date=depart,
        return_date=depart + timedelta(days=trip_length) if trip_length else None,
        price_inr=price,
        distance_km=3000,
    )


def test_record_and_history_roundtrip(store):
    store.record([_quote(10_000), _quote(11_000), _quote(9_500)])
    history = store.route_history("COK", "GYD", 10)
    assert sorted(history) == [9500.0, 10000.0, 11000.0]


def test_duplicate_observation_not_double_counted(store):
    q = _quote(10_000)
    store.record([q])
    n = store.record([q])
    assert n == 0
    assert len(store.route_history("COK", "GYD", 10)) == 1


def test_duplicate_one_way_observation_not_double_counted(store):
    q = _quote(4_000, trip_length=None)
    store.record([q])
    n = store.record([q])
    assert n == 0
    assert len(store.route_history("COK", "GYD", None)) == 1


def test_one_way_and_round_trip_history_kept_separate(store):
    store.record([_quote(10_000, trip_length=10), _quote(4_000, trip_length=None)])
    assert store.route_history("COK", "GYD", 10) == [10_000.0]
    assert store.route_history("COK", "GYD", None) == [4_000.0]


def test_should_alert_first_time_is_true(store):
    assert store.should_alert("COK", "GYD", 10, "2026-11-01", 9000, Tier.LOOT)


def test_should_alert_suppressed_within_cooldown(store):
    store.record_alert("COK", "GYD", 10, "2026-11-01", 9000, Tier.LOOT)
    assert not store.should_alert("COK", "GYD", 10, "2026-11-01", 8900, Tier.LOOT)


def test_should_alert_allows_a_real_redrop(store):
    store.record_alert("COK", "GYD", 10, "2026-11-01", 9000, Tier.LOOT)
    # More than 10% further down: should break through the cooldown.
    assert store.should_alert("COK", "GYD", 10, "2026-11-01", 7900, Tier.LOOT)


def test_should_alert_allows_a_tier_upgrade(store):
    store.record_alert("COK", "GYD", 10, "2026-11-01", 9000, Tier.STEAL)
    assert store.should_alert("COK", "GYD", 10, "2026-11-01", 8950, Tier.JACKPOT)


def test_should_alert_blocks_a_tier_downgrade_at_same_price(store):
    store.record_alert("COK", "GYD", 10, "2026-11-01", 9000, Tier.JACKPOT)
    assert not store.should_alert("COK", "GYD", 10, "2026-11-01", 8990, Tier.STEAL)


def test_prune_removes_old_observations(store):
    from datetime import datetime

    old = _quote(10_000)
    q = Quote(**{**old.__dict__, "observed_at": datetime.now(UTC) - timedelta(days=200)})
    store.record([q])
    assert store.observation_count() == 1
    store.prune(days=90)
    assert store.observation_count() == 0
