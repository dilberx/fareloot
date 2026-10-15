"""Poll providers, score what comes back, and decide what to alert on.

This is wiring, not logic: the judgement calls live in scoring.py and baseline.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from .baseline import robust_z
from .config import LowestFareRule, Watchlist
from .journey import ranking_cost_inr
from .links import aviasales, google_flights
from .models import Deal, Origin, Quote, Tier
from .providers.base import Provider
from .scoring import Scored, score
from .store import Store

DISCOVERY_PROMOTE_TIER = Tier.DEAL
CURSOR_KEY = "wishlist_cursor"
CALLS_PER_PAIR = 3  # two round-trip months plus one reverse one-way


def usable_quotes(wl: Watchlist, quotes: list[Quote]) -> list[Quote]:
    """Drop round trips whose length falls outside every configured trip band.

    One-way quotes are always kept; they back the return-leg gate.
    """
    return [q for q in quotes if q.trip_length is None or wl.band_for(q.trip_length)]


def _wishlist_slice(
    store: Store,
    wl: Watchlist,
    origins: dict[str, Origin],
    call_budget: int | None,
    calls_spent: int,
) -> tuple[list[tuple[str, str]], int, int]:
    """Which (origin, destination) pairs this run should price.

    The full product of origins and destinations costs several hundred calls,
    far more than one run can spend under the provider's hourly cap. Walking it
    in a fixed order would price the same first few origins forever and never
    reach the rest, so each run resumes from a cursor persisted in the store
    and wraps around. Every pair is covered once per cycle of runs.
    """
    destinations = list(dict.fromkeys(wl.destinations + store.promoted()))
    pairs = [(o, d) for o in origins for d in destinations if d != o]
    if not pairs:
        return pairs, 0, 0
    if call_budget is None:
        count = len(pairs)
    else:
        count = max(0, min(len(pairs), (call_budget - calls_spent) // CALLS_PER_PAIR))
    start = int(store.get_meta(CURSOR_KEY, "0") or 0) % len(pairs)
    return pairs, start, count


@dataclass
class PollResult:
    deals: list[Deal]
    to_alert: list[Deal]
    observations_recorded: int
    calls_made: int
    errors: list[str]
    calls_ok: int = 0

    @property
    def all_calls_failed(self) -> bool:
        return bool(self.errors) and self.calls_ok == 0


def _depart_candidates(today: date, months: int) -> list[str]:
    out = []
    cursor = today.replace(day=1)
    for _ in range(months):
        out.append(cursor.strftime("%Y-%m"))
        year, month = cursor.year, cursor.month
        month += 1
        if month > 12:
            month, year = 1, year + 1
        cursor = date(year, month, 1)
    return out


def _to_deal(
    quote: Quote,
    origin: Origin,
    scored: Scored,
    *,
    marker: str = "",
) -> Deal:
    return Deal(
        quote=quote,
        origin=origin,
        hotness=scored.hotness,
        tier=scored.tier,
        baseline_inr=scored.baseline_inr,
        saving_inr=scored.saving_inr,
        pct_below=scored.pct_below,
        one_way_only=scored.one_way_only,
        reasons=scored.reasons,
    )


def _return_leg_z(
    store: Store, quote: Quote, reverse_quotes: dict[tuple[str, str], list[Quote]]
) -> float | None:
    """How the return leg's own one-way market compares to its own history.

    Travelpayouts prices a round trip as a single total, not itemised legs, so
    the outbound-cheap/return-expensive trap can't be read off that number
    directly. Instead the watcher separately fetches the reverse-direction
    one-way price (destination -> origin) and scores *that* against its own
    one-way history. A quote missing here (no reverse fetch this pass) yields
    None, which scoring treats as reduced-but-not-zero confidence.
    """
    if quote.return_date is None:
        return None
    candidates = reverse_quotes.get((quote.origin, quote.destination), [])
    if not candidates:
        return None
    closest = min(candidates, key=lambda q: abs((q.depart_date - quote.return_date).days))
    history = store.route_history(quote.destination, quote.origin, None)
    return robust_z(closest.price_inr, history)


def _score_quote(
    store: Store,
    wl: Watchlist,
    origin: Origin,
    quote: Quote,
    reverse_quotes: dict[tuple[str, str], list[Quote]],
) -> Scored:
    band = wl.band_for(quote.trip_length) if quote.trip_length is not None else None
    history = store.route_history(quote.origin, quote.destination, quote.trip_length, band=band)
    return_z = _return_leg_z(store, quote, reverse_quotes)
    global_km = store.global_inr_per_km() if not history else []
    return score(
        price_inr=quote.price_inr,
        route_history=history,
        reach=origin.reach,
        max_reach_hours=wl.max_reach_hours,
        is_round_trip=quote.is_round_trip,
        return_z=return_z,
        inr_per_km=quote.inr_per_km,
        global_inr_per_km=global_km,
        expires_at=quote.expires_at,
        thresholds=wl.thresholds(),
    )


LOWEST_KEY = "lowest"  # alerts-table depart_date for route-level lowest-fare alerts


def _apply_lowest_rule(
    store: Store,
    wl: Watchlist,
    deal: Deal,
    scored: Scored,
    rule: LowestFareRule,
    as_of: datetime,
) -> None:
    """Promote a deal to STEAL if it is the cheapest the route has been lately.

    Only a round trip whose return leg was checked and found healthy qualifies;
    the rule loosens how deep a drop must be, never what counts as a real trip.
    """
    q = deal.quote
    band = wl.band_for(q.trip_length) if q.trip_length is not None else None
    if band is None or scored.gates.get("return_health") != 1.0:
        return
    if scored.gates.get("staleness", 1.0) == 0.0:
        return
    if scored.saving_inr < rule.min_saving_inr or scored.pct_below < rule.min_pct_below_baseline:
        return
    low, n, span_days = store.route_low(
        q.origin, q.destination, band, days=rule.window_days, before=as_of
    )
    if low is None or n < rule.min_history or span_days < rule.min_history_days:
        return
    if q.price_inr >= low:
        return
    deal.lowest_in_window = True
    if deal.tier.rank < Tier.STEAL.rank:
        deal.tier = Tier.STEAL
    deal.reasons.insert(
        0,
        f"cheapest {q.origin}->{q.destination} round trip in {rule.window_days} days "
        f"(previous low Rs {int(low):,})",
    )


def alert_key(deal: Deal) -> tuple[int | None, str]:
    """(trip_length, depart_date) used to cool alerts down.

    Lowest-fare alerts are about the route, not one departure date; keying them
    per date would let every date that dips below the old low alert separately.
    """
    if deal.lowest_in_window:
        return None, LOWEST_KEY
    return deal.quote.trip_length, deal.quote.depart_date.isoformat()


def score_quotes(
    store: Store,
    wl: Watchlist,
    quotes: list[Quote],
    origins: dict[str, Origin] | None = None,
    reverse_quotes: dict[tuple[str, str], list[Quote]] | None = None,
    *,
    marker: str = "",
    promote_discoveries: bool = True,
    as_of: datetime | None = None,
) -> list[Deal]:
    """Score a batch of quotes into ranked Deals.

    Shared by `poll` (scoring what was just fetched) and the `deals` CLI command
    (re-scoring what's already in the database, with no new API calls).
    """
    origins = origins if origins is not None else wl.origin_map()
    reverse_quotes = reverse_quotes or {}
    deals: list[Deal] = []
    for quote in usable_quotes(wl, quotes):
        origin = origins.get(quote.origin)
        if origin is None:
            continue
        scored = _score_quote(store, wl, origin, quote, reverse_quotes)
        deal = _to_deal(quote, origin, scored, marker=marker)
        rule = wl.lowest_rule_for(quote.destination)
        if rule is not None and as_of is not None:
            _apply_lowest_rule(store, wl, deal, scored, rule, as_of)
        deals.append(deal)
        if (
            promote_discoveries
            and scored.tier != Tier.NONE
            and quote.destination not in wl.destinations
            and scored.tier.rank >= DISCOVERY_PROMOTE_TIER.rank
        ):
            store.promote(quote.destination, scored.hotness)

    deals.sort(key=lambda d: d.hotness, reverse=True)
    return deals


SEED_CURSOR_KEY = "seed_cursor"
CALLS_PER_SEED_TASK = 2  # month matrix in both directions


@dataclass
class SeedResult:
    recorded: int
    calls_made: int
    tasks_done: int
    tasks_total: int
    start: int
    cycle_completed: bool
    errors: list[str]
    calls_ok: int = 0

    @property
    def all_calls_failed(self) -> bool:
        return bool(self.errors) and self.calls_ok == 0


def seed(
    provider: Provider,
    store: Store,
    wl: Watchlist,
    *,
    today: date | None = None,
    call_budget: int | None = None,
    only: list[str] | None = None,
) -> SeedResult:
    """Bootstrap baselines from month-matrix calendars, a rotating slice per run.

    Seeds both directions: origin->destination for the route baseline, and
    destination->origin so the return-leg gate has a reverse one-way baseline to
    judge a live reverse quote against. The full task list costs far more than
    one run's budget, so it resumes from its own cursor like poll does.
    """
    today = today or date.today()
    origins = wl.origin_map()
    months = _depart_candidates(today, 2)
    destinations = list(dict.fromkeys(wl.destinations))
    cursor_key = SEED_CURSOR_KEY
    if only:
        wanted = [d.upper() for d in only]
        unknown = sorted(set(wanted) - set(destinations))
        if unknown:
            raise ValueError(f"not in the watchlist's destinations: {', '.join(unknown)}")
        destinations = [d for d in destinations if d in wanted]
        # A filtered run has its own task list, so it gets its own cursor and
        # leaves the full cycle's position untouched.
        cursor_key = f"{SEED_CURSOR_KEY}:{','.join(sorted(wanted))}"
    tasks = [(o, d, m) for o in origins for d in destinations if d != o for m in months]
    if not tasks:
        return SeedResult(0, 0, 0, 0, 0, False, [])

    if call_budget is None:
        count = len(tasks)
    else:
        count = max(0, min(len(tasks), call_budget // CALLS_PER_SEED_TASK))
    start = int(store.get_meta(cursor_key, "0") or 0) % len(tasks)

    recorded = 0
    ok = 0
    errors: list[str] = []
    for i in range(count):
        origin_iata, destination, month = tasks[(start + i) % len(tasks)]
        for a, b in ((origin_iata, destination), (destination, origin_iata)):
            try:
                recorded += store.record(usable_quotes(wl, provider.month_matrix(a, b, month)))
                ok += 1
            except Exception as exc:
                errors.append(f"month_matrix {a}->{b} {month}: {exc}")
    # A run where every call failed (a revoked token, an outage) seeded nothing, so
    # it must not move the cursor past those tasks or report the cycle as done.
    worked = ok > 0 or not errors
    if worked:
        store.set_meta(cursor_key, str((start + count) % len(tasks)))

    return SeedResult(
        recorded=recorded,
        calls_made=getattr(provider, "calls_made", 0),
        tasks_done=count,
        tasks_total=len(tasks),
        start=start,
        cycle_completed=worked and start + count >= len(tasks),
        errors=errors,
        calls_ok=ok,
    )


def poll(
    provider: Provider,
    store: Store,
    wl: Watchlist,
    *,
    today: date | None = None,
    marker: str = "",
    include_discovery: bool = True,
    call_budget: int | None = None,
) -> PollResult:
    today = today or date.today()
    run_started = datetime.now(UTC)
    origins = wl.origin_map()
    months = _depart_candidates(today, wl.search_horizon_months)
    all_quotes: list[Quote] = []
    reverse_quotes: dict[tuple[str, str], list[Quote]] = {}
    errors: list[str] = []
    calls = 0
    ok = 0

    def attempt(label: str, fetch, *args, **kwargs) -> list[Quote]:
        # Provider errors must not kill the whole run, but they must be counted:
        # a run where nothing succeeded has to fail visibly, not go green empty.
        nonlocal ok
        try:
            quotes = fetch(*args, **kwargs)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            return []
        ok += 1
        return quotes

    # Discovery goes first: about two calls per origin buys coverage of every
    # destination, so it must never be starved by the wishlist spending the budget.
    if include_discovery:
        for origin in origins.values():
            calls += 2
            iata = origin.iata
            all_quotes += attempt(f"city_directions {iata}", provider.city_directions, iata)
            all_quotes += attempt(
                f"latest {iata}", provider.latest, iata, one_way=False, trip_duration=10
            )

    pairs, start, count = _wishlist_slice(store, wl, origins, call_budget, calls)
    for i in range(count):
        o, d = pairs[(start + i) % len(pairs)]
        for month in months[:2]:
            all_quotes += attempt(f"cheap {o}->{d}", provider.cheap, o, d, month, month)
        # One-way in the *return* direction, to judge whether the way back is a
        # healthy price or a trap - see _return_leg_z.
        reverse = attempt(f"reverse {d}->{o}", provider.cheap, d, o, months[0])
        reverse_quotes.setdefault((o, d), []).extend(reverse)
        all_quotes += reverse
    # Same rule as seed: if nothing succeeded, those pairs were never priced.
    if pairs and (ok > 0 or not errors):
        store.set_meta(CURSOR_KEY, str((start + count) % len(pairs)))

    all_quotes = usable_quotes(wl, all_quotes)
    recorded = store.record(all_quotes)
    deals = score_quotes(
        store, wl, all_quotes, origins, reverse_quotes, marker=marker, as_of=run_started
    )

    to_alert = []
    a = wl.alerts
    lowest_routes: set[tuple[str, str]] = set()
    # Cheapest first, so if several departure dates of one route are all a new
    # low this run, the one alert sent is the cheapest of them.
    for deal in sorted(deals, key=lambda d: (-d.tier.rank, d.quote.price_inr)):
        if not deal.tier.pushes:
            continue
        route = (deal.quote.origin, deal.quote.destination)
        if deal.lowest_in_window and route in lowest_routes:
            continue
        trip_key, date_key = alert_key(deal)
        if store.should_alert(
            deal.quote.origin,
            deal.quote.destination,
            trip_key,
            date_key,
            deal.quote.price_inr,
            deal.tier,
            cooldown_hours=a.cooldown_hours,
            redrop_pct=a.redrop_pct,
        ):
            to_alert.append(deal)
            if deal.lowest_in_window:
                lowest_routes.add(route)

    calls_made = getattr(provider, "calls_made", 0)
    return PollResult(
        deals=deals,
        to_alert=to_alert,
        observations_recorded=recorded,
        calls_made=calls_made,
        errors=errors,
        calls_ok=ok,
    )


def record_alerts(store: Store, deals: list[Deal]) -> None:
    """Mark deals as alerted, so the cooldown suppresses repeats.

    Call this only for deals that were actually delivered. Recording before
    sending meant a failed Telegram call left the deal marked as sent, and the
    cooldown then hid the real deal for 48 hours.
    """
    for deal in deals:
        trip_key, date_key = alert_key(deal)
        store.record_alert(
            deal.quote.origin,
            deal.quote.destination,
            trip_key,
            date_key,
            deal.quote.price_inr,
            deal.tier,
        )


def summarize_errors(errors: list[str]) -> list[tuple[str, int]]:
    """Group errors by cause with the route stripped, for public logs.

    Error strings start with the route ("cheap COK->BKK: ..."), and route pairs
    reveal the private watchlist.
    """
    counts: dict[str, int] = {}
    for err in errors:
        cause = err.split(": ", 1)[1] if ": " in err else err
        counts[cause] = counts.get(cause, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def ranked_total(deal: Deal, wl: Watchlist) -> float:
    return ranking_cost_inr(deal.quote.price_inr, deal.origin.reach, wl.train_hour_value_inr)


def format_alert_lines(deal: Deal, marker: str = "") -> list[str]:
    q = deal.quote
    lines = [f"{deal.tier.value}  {deal.hotness}   {q.origin} -> {q.destination}"]
    if q.is_round_trip:
        out_str = f"{q.depart_date:%d %b}"
        back_str = f"{q.return_date:%d %b}"
        lines.append(
            f"round trip Rs {q.price_inr:,}  (out {out_str} / back {back_str}, {q.trip_length}d)"
        )
    else:
        lines.append(f"one way Rs {q.price_inr:,}  ({q.depart_date:%d %b})")
    if deal.live_price_inr is not None:
        lines.append(f"  live on Google Flights: Rs {deal.live_price_inr:,}")
    elif deal.live_note:
        lines.append(f"  {deal.live_note}")
    for reason in deal.reasons:
        lines.append(f"  {reason}")
    lines.append(f"  via {aviasales(q, marker)}")
    lines.append(f"  or {google_flights(q)}")
    return lines
