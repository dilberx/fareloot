from datetime import UTC, date, datetime, timedelta

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
    record_alerts,
    seed,
    summarize_errors,
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
        # MockProvider seeds two months of history; pricing six would let a run's
        # crashed prices dilute the baseline the next run judges them against.
        search_horizon_months=2,
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
    record_alerts(store, r1.to_alert)  # the caller records what it delivered

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
    assert "Kochi to Baku" in text
    assert "Rs 7,500" in text
    assert "the usual Rs 18,000" in text
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
        # These tests pin the near-month rotation; far months get their own tests.
        far_month_share=0.0,
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


class _SeedRecordingProvider(MockProvider):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.matrices: set[tuple[str, str, str]] = set()

    def month_matrix(self, origin, destination, month):
        self.matrices.add((origin, destination, month))
        return super().month_matrix(origin, destination, month)


def test_seed_rotates_until_every_route_is_seeded_both_ways(store):
    wl = _multi_origin_watchlist(["GYD", "TBS"])  # 5 origins x 2 dests x 2 months = 20 tasks
    today = date(2026, 11, 1)
    seen: set[tuple[str, str, str]] = set()
    results = []
    for _ in range(4):  # 6 calls -> 3 tasks per run; 4 runs is not enough...
        p = _SeedRecordingProvider(today=today)
        results.append(seed(p, store, wl, today=today, call_budget=6))
        seen |= p.matrices
    assert not any(r.cycle_completed for r in results)
    for _ in range(3):  # ...7 runs is
        p = _SeedRecordingProvider(today=today)
        results.append(seed(p, store, wl, today=today, call_budget=6))
        seen |= p.matrices
    assert results[-1].cycle_completed
    forward = {(o, d) for o, d, _ in seen if o in {"COK", "TRV", "CCJ", "BLR", "MAA"}}
    reverse = {(d, o) for o, d, _ in seen if d in {"COK", "TRV", "CCJ", "BLR", "MAA"}}
    assert {o for o, _ in forward} == {"COK", "TRV", "CCJ", "BLR", "MAA"}
    # The return-leg gate needs the reverse one-way baseline for every route.
    assert forward == reverse


def test_seed_respects_budget_and_unbounded_seeds_everything(store):
    wl = _multi_origin_watchlist(["GYD", "TBS"])
    today = date(2026, 11, 1)
    p = _SeedRecordingProvider(today=today)
    r = seed(p, store, wl, today=today, call_budget=10)
    assert p.calls_made <= 10
    assert r.tasks_done == 5

    p2 = _SeedRecordingProvider(today=today)
    r2 = seed(p2, store, wl, today=today)
    assert r2.tasks_done == r2.tasks_total == 20


def test_same_day_and_overnight_round_trips_are_never_recorded_or_scored(store):
    from fareloot.watcher import usable_quotes

    wl = _watchlist(["GYD"])
    d = date(2026, 12, 1)

    def q(days):
        return Quote(
            origin="COK",
            destination="GYD",
            depart_date=d,
            return_date=d.replace(day=1 + days),
            price_inr=5000,
            distance_km=3000,
        )

    one_way = Quote(origin="GYD", destination="COK", depart_date=d, price_inr=4000)
    kept = usable_quotes(wl, [q(0), q(1), q(2), q(3), q(9), one_way])
    assert sorted(x.trip_length for x in kept if x.trip_length is not None) == [3, 9]
    assert one_way in kept


def test_baseline_pools_the_whole_trip_band(store):
    wl = _watchlist(["GYD"])
    d = date(2026, 12, 1)
    history = [
        Quote(
            origin="COK",
            destination="GYD",
            depart_date=d.replace(day=1 + i),
            return_date=d.replace(day=1 + i + length),
            price_inr=20_000 + 100 * i,
        )
        for i, length in enumerate([7, 8, 10, 7, 8, 10, 7, 8])
    ]
    store.record(history)
    # A 9-day quote has no 9-day history at all, but the 7-10 band does.
    assert store.route_history("COK", "GYD", 9) == []
    assert len(store.route_history("COK", "GYD", 9, band=wl.band_for(9))) == 8


def test_undelivered_alerts_are_not_suppressed_next_run(store):
    # Alerts used to be recorded inside poll(), before Telegram was called. A
    # failed send left them marked as delivered and the cooldown hid the deal.
    wl = _watchlist(["GYD"])
    today = date(2026, 11, 1)
    provider = MockProvider(today=today)
    _seed_route(store, provider, "COK", "GYD", ["2026-11", "2026-12"])

    first = poll(provider, store, wl, today=today)
    assert first.to_alert
    # ...and the send fails, so nothing is recorded. Every deal that still
    # qualifies next run must alert again; none may be held back by a cooldown.
    second = poll(provider, store, wl, today=today)
    still_qualifying = [d for d in second.deals if d.tier.pushes]
    assert still_qualifying
    assert len(second.to_alert) == len(still_qualifying)


class _DeadTokenProvider(MockProvider):
    def _boom(self, *a, **kw):
        self.calls_made += 1
        raise RuntimeError("returned HTTP 401")

    cheap = month_matrix = city_directions = latest = _boom


def test_dead_token_poll_reports_failure_and_keeps_cursor(store):
    wl = _multi_origin_watchlist(["GYD", "TBS"])
    today = date(2026, 11, 1)
    r = poll(_DeadTokenProvider(today=today), store, wl, today=today, call_budget=30)
    assert r.all_calls_failed
    assert r.observations_recorded == 0
    assert store.get_meta("wishlist_cursor", "0") == "0"


def test_dead_token_seed_does_not_claim_a_cycle(store):
    wl = _multi_origin_watchlist(["GYD"])
    today = date(2026, 11, 1)
    r = seed(_DeadTokenProvider(today=today), store, wl, today=today)  # unbounded: whole cycle
    assert r.all_calls_failed
    assert not r.cycle_completed
    assert store.get_meta("seed_cursor", "0") == "0"


def test_partial_failure_is_not_reported_as_total(store):
    class HalfDead(MockProvider):
        def city_directions(self, origin):
            raise RuntimeError("returned HTTP 503")

    wl = _multi_origin_watchlist(["GYD"])
    today = date(2026, 11, 1)
    r = poll(HalfDead(today=today), store, wl, today=today, call_budget=30)
    assert r.errors and not r.all_calls_failed


def test_error_summary_strips_routes():
    errors = [
        "cheap COK->BKK: returned HTTP 503",
        "reverse GYD->BLR: returned HTTP 503",
        "latest MAA: call budget of 140 exhausted",
    ]
    summary = summarize_errors(errors)
    assert summary == [("returned HTTP 503", 2), ("call budget of 140 exhausted", 1)]
    text = " ".join(cause for cause, _ in summary)
    for code in ("COK", "BKK", "GYD", "BLR", "MAA"):
        assert code not in text


def test_seed_can_target_new_destinations_without_moving_the_main_cursor(store):
    wl = _multi_origin_watchlist(["GYD", "TBS", "SHJ", "RKT"])
    today = date(2026, 11, 1)
    store.set_meta("seed_cursor", "7")
    p = _SeedRecordingProvider(today=today)
    r = seed(p, store, wl, today=today, call_budget=1000, only=["shj", "rkt"])
    # 5 origins x 2 destinations x 2 months, each seeded in both directions.
    assert r.tasks_total == 20 and r.cycle_completed
    assert {d for _, d, _ in p.matrices if d not in {"COK", "TRV", "CCJ", "BLR", "MAA"}} == {
        "SHJ",
        "RKT",
    }
    assert {o for o, _, _ in p.matrices if o not in {"COK", "TRV", "CCJ", "BLR", "MAA"}} == {
        "SHJ",
        "RKT",
    }
    assert store.get_meta("seed_cursor") == "7"


def test_seed_rejects_destinations_outside_the_watchlist(store):
    wl = _multi_origin_watchlist(["GYD"])
    with pytest.raises(ValueError, match="SHJ"):
        seed(MockProvider(), store, wl, only=["SHJ"])


class _MonthRecordingProvider(MockProvider):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.round_trips: set[tuple[str, str, str]] = set()
        self.reverses: set[tuple[str, str, str]] = set()

    def cheap(self, origin, destination, depart, return_=None):
        (self.round_trips if return_ else self.reverses).add((origin, destination, depart[:7]))
        return super().cheap(origin, destination, depart, return_)


def _far_watchlist(share: float) -> Watchlist:
    wl = _multi_origin_watchlist(["GYD", "TBS"])
    wl.far_month_share = share
    return wl


def test_far_months_are_all_priced_across_runs_with_their_own_reverse_leg(store):
    wl = _far_watchlist(0.5)  # 10 pairs x 4 far months = 40 far items
    today = date(2026, 11, 1)
    far = {"2027-01", "2027-02", "2027-03", "2027-04"}
    rt: set[tuple[str, str, str]] = set()
    rev: set[tuple[str, str, str]] = set()
    for _ in range(10):
        p = _MonthRecordingProvider(today=today)
        poll(p, store, wl, today=today, include_discovery=False, call_budget=20)
        rt |= p.round_trips
        rev |= p.reverses
    far_rt = {x for x in rt if x[2] in far}
    assert len(far_rt) == 40
    # Each far month's return leg is judged against a reverse fetch in that month.
    assert {(d, o, m) for o, d, m in far_rt} <= rev
    # The near months keep being priced too.
    assert {m for _, _, m in rt} >= {"2026-11", "2026-12"}


def test_far_share_zero_never_prices_far_months(store):
    p = _MonthRecordingProvider(today=date(2026, 11, 1))
    poll(
        p,
        store,
        _far_watchlist(0.0),
        today=date(2026, 11, 1),
        include_discovery=False,
        call_budget=60,
    )
    assert {m for _, _, m in p.round_trips} <= {"2026-11", "2026-12"}


def test_far_months_respect_the_budget(store):
    p = _MonthRecordingProvider(today=date(2026, 11, 1))
    poll(
        p,
        store,
        _far_watchlist(0.5),
        today=date(2026, 11, 1),
        include_discovery=True,
        call_budget=40,
    )
    assert p.calls_made <= 40


def test_run_does_not_count_its_own_prices_as_baseline(store):
    # A route crashing on every date at once must still read as a crash.
    wl = _multi_origin_watchlist(["GYD"])
    wl.far_month_share = 0.5
    today = date(2026, 11, 1)
    provider = MockProvider(today=today)
    _seed_route(store, provider, "COK", "GYD", ["2026-11", "2026-12"])
    result = poll(provider, store, wl, today=today, include_discovery=False)
    assert any(d.quote.destination == "GYD" and d.tier.pushes for d in result.deals)


def test_departed_flights_are_never_recorded_or_alerted():
    from fareloot.watcher import usable_quotes

    wl = _watchlist(["GYD"])
    today = date(2026, 10, 17)
    past = Quote(
        origin="COK",
        destination="GYD",
        depart_date=date(2026, 10, 5),
        return_date=date(2026, 10, 15),
        price_inr=5000,
    )
    future = Quote(
        origin="COK",
        destination="GYD",
        depart_date=date(2026, 11, 5),
        return_date=date(2026, 11, 15),
        price_inr=5000,
    )
    departed_one_way = Quote(
        origin="GYD", destination="COK", depart_date=date(2026, 10, 16), price_inr=4000
    )
    assert usable_quotes(wl, [past, future, departed_one_way], today) == [future]


class _YieldProvider(MockProvider):
    """Round trips for every route except those scripted as empty or failing."""

    def __init__(self, empty=(), failing=(), **kw):
        super().__init__(**kw)
        self.empty, self.failing = set(empty), set(failing)
        self.rt_calls: list[tuple[str, str]] = []

    def cheap(self, origin, destination, depart, return_=None):
        if return_:
            self.rt_calls.append((origin, destination))
            if (origin, destination) in self.failing:
                raise RuntimeError("returned HTTP 503")
            if (origin, destination) in self.empty:
                return []
        return super().cheap(origin, destination, depart, return_)


TODAY = date(2026, 11, 1)


def _run(store, wl, **kw):
    p = _YieldProvider(today=TODAY, **kw)
    result = poll(p, store, wl, today=TODAY, include_discovery=False)
    return p, result


def test_route_that_keeps_returning_nothing_is_skipped(store):
    wl = _multi_origin_watchlist(["GYD", "TBS"])
    for _ in range(3):
        p, _ = _run(store, wl, empty={("COK", "GYD")})
        assert ("COK", "GYD") in p.rt_calls
    p, result = _run(store, wl, empty={("COK", "GYD")})
    assert ("COK", "GYD") not in p.rt_calls
    assert ("COK", "TBS") in p.rt_calls  # everything else still priced
    assert result.dry_skipped >= 1


def test_dry_route_is_rechecked_after_a_week(store):
    wl = _multi_origin_watchlist(["GYD"])
    for _ in range(3):
        _run(store, wl, empty={("COK", "GYD")})
    week_ago = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    store.conn.execute("UPDATE route_yield SET last_attempt=?", (week_ago,))
    store.conn.commit()
    p, _ = _run(store, wl)  # the route has data again
    assert ("COK", "GYD") in p.rt_calls
    row = store.conn.execute(
        "SELECT misses_in_a_row FROM route_yield WHERE origin='COK' AND destination='GYD'"
    ).fetchone()
    assert row["misses_in_a_row"] == 0


def test_one_hit_resets_the_streak(store):
    wl = _multi_origin_watchlist(["GYD"])
    for _ in range(2):
        _run(store, wl, empty={("COK", "GYD")})
    _run(store, wl)
    for _ in range(2):
        _run(store, wl, empty={("COK", "GYD")})
    p, _ = _run(store, wl, empty={("COK", "GYD")})
    assert ("COK", "GYD") in p.rt_calls  # 2 misses since the hit, not 4


def test_failing_calls_never_mark_a_route_dry(store):
    # An outage or a bad token must not retire every route.
    wl = _multi_origin_watchlist(["GYD"])
    for _ in range(5):
        _run(store, wl, failing={("COK", "GYD")})
    p, _ = _run(store, wl)
    assert ("COK", "GYD") in p.rt_calls


def test_budget_freed_by_dry_routes_prices_more_live_ones(store):
    wl = _multi_origin_watchlist(["GYD", "TBS"])  # 10 pairs
    dead = {(o, "GYD") for o in ("COK", "TRV", "CCJ", "BLR", "MAA")}
    for _ in range(3):
        p = _YieldProvider(today=TODAY, empty=dead)
        poll(p, store, wl, today=TODAY, include_discovery=False)
    p = _YieldProvider(today=TODAY, empty=dead)
    poll(p, store, wl, today=TODAY, include_discovery=False, call_budget=15)  # 5 pairs' worth
    priced = set(p.rt_calls)
    assert priced and priced.isdisjoint(dead)
    assert len(priced) == 5  # all five live routes, none wasted on dead ones
