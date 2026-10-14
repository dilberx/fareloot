from datetime import date

import pytest

from fareloot.config import OriginModel, ReachModel, Watchlist
from fareloot.models import Deal, Origin, Quote, Reach, Tier
from fareloot.providers.mock import MockProvider
from fareloot.store import Store
from fareloot.watcher import (
    _depart_candidates,
    format_alert_lines,
    poll,
    ranked_total,
)


def _watchlist(destinations: list[str] | None = None) -> Watchlist:
    return Watchlist(
        origins=[
            OriginModel(
                iata="COK",
                name="Kochi",
                reach=ReachModel(mode="local", hours=1, cost_inr=200),
            )
        ],
        destinations=destinations or ["GYD", "TBS"],
        max_reach_hours=14,
        train_hour_value_inr=80,
    )


def _seed_route(store: Store, provider: MockProvider, origin: str, dest: str, months: list[str]):
    for month in months:
        for a, b in ((origin, dest), (dest, origin)):
            quotes = provider.month_matrix(a, b, month)
            store.record(quotes)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


def test_depart_candidates_handles_year_boundary():
    candidates = _depart_candidates(date(2026, 11, 20), 4)
    assert candidates == ["2026-11", "2026-12", "2027-01", "2027-02"]


def test_poll_detects_crash_and_blocks_return_trap(store):
    wl = _watchlist(["GYD", "TBS"])
    today = date(2026, 11, 1)
    provider = MockProvider(today=today)

    _seed_route(store, provider, "COK", "GYD", ["2026-11", "2026-12"])
    _seed_route(store, provider, "COK", "TBS", ["2026-11", "2026-12"])

    result = poll(provider, store, wl, today=today)

    alert_routes = [(d.quote.origin, d.quote.destination) for d in result.to_alert]
    assert ("COK", "GYD") in alert_routes
    assert ("COK", "TBS") not in alert_routes

    tbs_deals = [d for d in result.deals if d.quote.destination == "TBS"]
    assert tbs_deals
    assert all(d.tier.rank <= Tier.DEAL.rank for d in tbs_deals)


def test_repeated_poll_does_not_duplicate_one_way_observations(store):
    # Regression test: one-way quotes (trip_length IS NULL) back the return-leg
    # baseline. SQLite NULL uniqueness previously allowed duplicate one-way rows
    # on repeated polls within the same day, which would inflate history.
    wl = _watchlist(["GYD"])
    today = date(2026, 11, 1)
    provider = MockProvider(today=today)

    r1 = poll(provider, store, wl, today=today)
    assert r1.observations_recorded > 0

    one_ways_1 = store.conn.execute(
        "SELECT COUNT(*) FROM observations WHERE trip_length IS NULL"
    ).fetchone()[0]
    round_trips_1 = store.conn.execute(
        "SELECT COUNT(*) FROM observations WHERE trip_length IS NOT NULL"
    ).fetchone()[0]
    assert one_ways_1 > 0
    assert round_trips_1 > 0

    r2 = poll(provider, store, wl, today=today)
    assert r2.observations_recorded == 0

    one_ways_2 = store.conn.execute(
        "SELECT COUNT(*) FROM observations WHERE trip_length IS NULL"
    ).fetchone()[0]
    round_trips_2 = store.conn.execute(
        "SELECT COUNT(*) FROM observations WHERE trip_length IS NOT NULL"
    ).fetchone()[0]

    assert one_ways_2 == one_ways_1
    assert round_trips_2 == round_trips_1


def test_provider_errors_are_isolated_and_recorded(store):
    class FlakyProvider(MockProvider):
        def cheap(self, origin: str, destination: str, depart: str, return_: str | None = None):
            if destination == "GYD":
                raise RuntimeError("upstream timeout")
            return super().cheap(origin, destination, depart, return_)

    wl = _watchlist(["GYD", "TBS"])
    today = date(2026, 11, 1)
    provider = FlakyProvider(today=today)

    result = poll(provider, store, wl, today=today)

    assert any("upstream timeout" in err for err in result.errors)
    assert any(d.quote.destination == "TBS" for d in result.deals)


def test_discovery_promotes_hot_destinations_and_can_be_disabled(store):
    wl = _watchlist(["GYD"])
    today = date(2026, 11, 1)
    provider = MockProvider(today=today)

    result_no_disc = poll(provider, store, wl, today=today, include_discovery=False)
    discovered_destinations = [
        d.quote.destination for d in result_no_disc.deals if d.quote.destination != "GYD"
    ]
    assert len(discovered_destinations) == 0

    result_disc = poll(provider, store, wl, today=today, include_discovery=True)
    assert result_disc.observations_recorded > 0
    promoted = store.promoted()
    assert isinstance(promoted, list)


def test_cooldown_suppresses_repeat_alerts_until_redrop(store):
    wl = _watchlist(["GYD"])
    today = date(2026, 11, 1)
    provider = MockProvider(today=today)

    _seed_route(store, provider, "COK", "GYD", ["2026-11", "2026-12"])

    r1 = poll(provider, store, wl, today=today)
    gyd_alerts = [d for d in r1.to_alert if d.quote.destination == "GYD"]
    assert len(gyd_alerts) > 0

    r2 = poll(provider, store, wl, today=today)
    gyd_alerts_2 = [d for d in r2.to_alert if d.quote.destination == "GYD"]
    assert len(gyd_alerts_2) == 0


def test_format_alert_lines_includes_links_and_reasons():
    quote = Quote(
        origin="COK",
        destination="GYD",
        depart_date=date(2026, 11, 10),
        return_date=date(2026, 11, 20),
        price_inr=7500,
    )
    origin = Origin(iata="COK", name="Kochi", reach=Reach(mode="local", hours=1, cost_inr=200))
    deal = Deal(
        quote=quote,
        origin=origin,
        hotness=88,
        tier=Tier.LOOT,
        baseline_inr=18000,
        saving_inr=10500,
        pct_below=58.3,
        one_way_only=False,
        reasons=["usually Rs 18,000 (58% below)", "return leg normal (z -0.1)"],
    )

    lines = format_alert_lines(deal, marker="affiliate123")
    text = "\n".join(lines)
    assert "LOOT" in text
    assert "COK -> GYD" in text
    assert "Rs 7,500" in text
    assert "usually Rs 18,000" in text
    assert "aviasales.com" in text
    assert "marker=affiliate123" in text
    assert "google.com/travel/flights" in text


def test_ranked_total_matches_journey_calculation():
    quote = Quote(
        origin="COK",
        destination="GYD",
        depart_date=date(2026, 11, 10),
        price_inr=10000,
    )
    origin = Origin(
        iata="COK",
        name="Kochi",
        reach=Reach(mode="train", hours=4.0, cost_inr=500),
    )
    deal = Deal(
        quote=quote,
        origin=origin,
        hotness=70,
        tier=Tier.STEAL,
        baseline_inr=15000,
        saving_inr=5000,
        pct_below=33.3,
        one_way_only=True,
        reasons=[],
    )
    wl = Watchlist(
        origins=[],
        destinations=[],
        train_hour_value_inr=100.0,
    )
    assert ranked_total(deal, wl) == 11800.0


def _multi_origin_watchlist(destinations: list[str]) -> Watchlist:
    return Watchlist(
        origins=[
            OriginModel(iata=code, name=code, reach=ReachModel(mode="train", hours=h, cost_inr=500))
            for code, h in [("COK", 1), ("TRV", 4), ("CCJ", 4), ("BLR", 12), ("MAA", 14)]
        ],
        destinations=destinations,
        max_reach_hours=14,
        train_hour_value_inr=80,
    )


class _RecordingProvider(MockProvider):
    """Remembers which round-trip routes were priced and how many calls were made."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.priced: set[tuple[str, str]] = set()

    def cheap(self, origin, destination, depart, return_=None):
        if return_ is not None:
            self.priced.add((origin, destination))
        return super().cheap(origin, destination, depart, return_)


def test_small_budget_still_reaches_every_origin_across_runs(store):
    # 5 origins x 4 destinations = 20 pairs, but a budget that only fits 3 per run.
    # The old fixed-order loop priced the first origin forever and never reached
    # BLR or MAA, the overnight-train airports the reach ranking exists for.
    wl = _multi_origin_watchlist(["GYD", "TBS", "BKK", "DXB"])
    today = date(2026, 11, 1)
    covered: set[tuple[str, str]] = set()
    for _ in range(7):
        p = _RecordingProvider(today=today)
        poll(p, store, wl, today=today, include_discovery=False, call_budget=9)
        covered |= p.priced
    origins_reached = {o for o, _ in covered}
    assert origins_reached == {"COK", "TRV", "CCJ", "BLR", "MAA"}
    assert len(covered) == 20


def test_cursor_wraps_and_never_repeats_within_a_cycle(store):
    wl = _multi_origin_watchlist(["GYD", "TBS"])  # 10 pairs
    today = date(2026, 11, 1)
    seen: list[tuple[str, str]] = []
    for _ in range(5):  # 2 pairs per run -> exactly one full cycle
        p = _RecordingProvider(today=today)
        poll(p, store, wl, today=today, include_discovery=False, call_budget=6)
        seen += sorted(p.priced)
    assert len(seen) == 10
    assert len(set(seen)) == 10


def test_discovery_runs_even_when_budget_is_tiny(store):
    # Discovery is spent first, so a budget that leaves nothing for the wishlist
    # must still scan every origin.
    wl = _multi_origin_watchlist(["GYD"])
    today = date(2026, 11, 1)
    p = _RecordingProvider(today=today)
    result = poll(p, store, wl, today=today, include_discovery=True, call_budget=10)
    discovered = {d.quote.origin for d in result.deals if d.quote.destination != "GYD"}
    assert discovered == {"COK", "TRV", "CCJ", "BLR", "MAA"}
    assert p.priced == set()


def test_budget_caps_calls(store):
    wl = _multi_origin_watchlist(["GYD", "TBS", "BKK", "DXB"])
    today = date(2026, 11, 1)
    p = _RecordingProvider(today=today)
    poll(p, store, wl, today=today, include_discovery=True, call_budget=40)
    assert p.calls_made <= 40
