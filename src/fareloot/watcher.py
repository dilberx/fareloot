"""Poll providers, score what comes back, and decide what to alert on.

This is wiring, not logic: the judgement calls live in scoring.py and baseline.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from .baseline import median, robust_z
from .config import LowestFareRule, Watchlist
from .geo import km
from .journey import ranking_cost_inr, total_cost_inr, trip_window, worth_the_detour
from .models import Deal, Origin, Quote, Tier
from .places import place
from .providers.base import Provider
from .scoring import Scored, score
from .store import Store

DISCOVERY_PROMOTE_TIER = Tier.DEAL
CURSOR_KEY = "wishlist_cursor"
FAR_CURSOR_KEY = "far_month_cursor"
NEAR_MONTHS = 2
CALLS_PER_FAR_ITEM = 2  # one round-trip month plus its reverse one-way
# A wishlist route whose round-trip calls succeed but return nothing usable this
# many times in a row is skipped, then re-checked once it has rested this long.
# Measured: four small origins and five destinations returned ~6% of round
# trips while costing ~60% of the wishlist budget.
DRY_AFTER_MISSES = 3
DRY_RECHECK_DAYS = 7
# Door-to-door comparison against other airports: an airport's usual price
# counts once it has this many observations in the band, and this run's fares
# from other airports count when they leave within this many days of the deal.
ALT_MIN_HISTORY = 5
ALT_DATE_WINDOW_DAYS = 3
MAX_OPTIONS = 4


def _distance(origin: str, destination: str, quote: Quote | None = None) -> int | None:
    found = km(origin, destination)
    if found is None and quote is not None:
        return quote.distance_km
    return found


def _worth_it(origin: Origin, destination: str, quote: Quote | None = None) -> bool:
    return worth_the_detour(origin.reach, _distance(origin.iata, destination, quote))


def _walk(items: list, start: int, count: int, skip) -> tuple[list, int, int]:
    """Take up to `count` items from `start`, stepping past skipped ones.

    Returns (chosen, next cursor, skipped). The list itself never shrinks, so the
    cursor keeps its meaning as routes go dry and come back.
    """
    chosen: list = []
    skipped = 0
    i = 0
    while len(chosen) < count and i < len(items):
        item = items[(start + i) % len(items)]
        i += 1
        if skip(item):
            skipped += 1
            continue
        chosen.append(item)
    return chosen, ((start + i) % len(items) if items else 0), skipped


CALLS_PER_PAIR = 3  # two round-trip months plus one reverse one-way


def usable_quotes(wl: Watchlist, quotes: list[Quote], today: date | None = None) -> list[Quote]:
    """Drop quotes no one could book: past departures, and round trips whose
    length falls outside every configured trip band.

    Cached prices live for days, so a fare searched last week for a flight that
    has since left can still come back from the provider.
    One-way quotes in the future are always kept; they back the return-leg gate.
    """
    today = today or date.today()
    return [
        q
        for q in quotes
        if q.depart_date >= today and (q.trip_length is None or wl.band_for(q.trip_length))
    ]


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
    pairs = [(o, d) for o in origins for d in destinations if d != o and _worth_it(origins[o], d)]
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
    # Wishlist route checks skipped because the route keeps returning nothing.
    dry_skipped: int = 0

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
        return_status=_RETURN_STATUS.get(scored.gates.get("return_health"), "unchecked"),
    )


# Gate values set in scoring.score(): 1.0 checked and healthy, 0.5 overpriced
# return, 0.7 no priced return at all, 0.85 round trip whose return wasn't checked.
_RETURN_STATUS = {1.0: "healthy", 0.5: "overpriced", 0.7: "one-way"}


def _return_leg_z(
    store: Store,
    quote: Quote,
    reverse_quotes: dict[tuple[str, str], list[Quote]],
    as_of: datetime | None = None,
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
    history = store.route_history(quote.destination, quote.origin, None, before=as_of)
    return robust_z(closest.price_inr, history)


def _score_quote(
    store: Store,
    wl: Watchlist,
    origin: Origin,
    quote: Quote,
    reverse_quotes: dict[tuple[str, str], list[Quote]],
    as_of: datetime | None = None,
) -> Scored:
    band = wl.band_for(quote.trip_length) if quote.trip_length is not None else None
    history = store.route_history(
        quote.origin, quote.destination, quote.trip_length, band=band, before=as_of
    )
    return_z = _return_leg_z(store, quote, reverse_quotes, as_of)
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
    deal.lowest_window_days = rule.window_days
    if deal.tier.rank < Tier.STEAL.rank:
        deal.tier = Tier.STEAL
    deal.reasons.insert(
        0,
        f"cheapest {q.origin}->{q.destination} round trip in {rule.window_days} days "
        f"(previous low Rs {int(low):,})",
    )


def alert_key(deal: Deal) -> tuple[int | None, str]:
    """(trip_length, depart_date) used to cool alerts down.

    One alert covers a route's whole month (its other dates ride along as
    options), so the cooldown is per month: keyed per date, every date in the
    month would alert on its own, run after run. Lowest-fare alerts are about
    the route, so they cool down per route.
    """
    if deal.lowest_in_window:
        return None, LOWEST_KEY
    return None, f"month:{deal.quote.depart_month}"


def pick_leads(deals: list[Deal], wl: Watchlist) -> list[Deal]:
    """One deal per destination and departure month, with its other dates attached.

    The lead is the pushing deal that is cheapest door to door; shorter trips
    win ties, since a long trip is the bigger commitment. Its options are the
    cheapest other dates on the same route and month, any length in a band, so
    the reader sees the flexible choices this run found.
    """
    groups: dict[tuple[str, str], list[Deal]] = {}
    for deal in deals:
        if deal.tier.pushes:
            key = (deal.quote.destination, deal.quote.depart_month)
            groups.setdefault(key, []).append(deal)
    leads = []
    for group in groups.values():
        lead = min(group, key=lambda d: (ranked_total(d, wl), d.quote.trip_length or 0))
        q = lead.quote
        route = (q.origin, q.destination, q.depart_month)
        seen = {(q.depart_date, q.return_date)}
        options: list[Quote] = []
        for other in sorted(deals, key=lambda d: d.quote.price_inr):
            o = other.quote
            if (o.origin, o.destination, o.depart_month) != route or not o.is_round_trip:
                continue
            if (o.depart_date, o.return_date) in seen:
                continue
            seen.add((o.depart_date, o.return_date))
            options.append(o)
            if len(options) == MAX_OPTIONS:
                break
        lead.options = options
        leads.append(lead)
    leads.sort(key=lambda d: (-d.tier.rank, d.quote.price_inr))
    return leads


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
        # Discovery happily returns Bangalore -> Hyderabad; nobody takes an
        # overnight train for that hop, so it is never a deal for this user.
        if origin is None or not _worth_it(origin, quote.destination, quote):
            continue
        scored = _score_quote(store, wl, origin, quote, reverse_quotes, as_of)
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

    for deal in deals:
        if deal.tier.pushes:
            _hold_back(store, wl, deal, deals, origins, as_of)
    deals.sort(key=lambda d: d.hotness, reverse=True)
    return deals


def _hold_back(
    store: Store,
    wl: Watchlist,
    deal: Deal,
    deals: list[Deal],
    origins: dict[str, Origin],
    as_of: datetime | None,
) -> None:
    """Demote a pushing deal that is a good fare but a bad trip.

    Scoring judges a fare against its own route. The trip is judged here: how
    long it is for the distance, and whether getting there another way usually
    costs less door to door.
    """
    q = deal.quote
    lo, hi = trip_window(_distance(q.origin, q.destination, q))
    if q.trip_length is not None and not lo <= q.trip_length <= hi:
        _demote(deal, f"{q.trip_length} nights is outside {lo}-{hi} for this distance")
        return
    why = _cheaper_elsewhere(store, wl, deal, deals, origins, as_of)
    if why:
        _demote(deal, why)


def _demote(deal: Deal, reason: str) -> None:
    deal.tier = Tier.NONE
    deal.lowest_in_window = False
    deal.reasons.append(reason)


def _cheaper_elsewhere(
    store: Store,
    wl: Watchlist,
    deal: Deal,
    deals: list[Deal],
    origins: dict[str, Origin],
    as_of: datetime | None,
) -> str | None:
    """Why another airport is the better way to this destination, or None.

    A Bangalore fare 40% below Bangalore's usual is no deal if flying from
    Kochi usually costs less once the train is counted. Two checks: other
    airports' usual door-to-door price, and this run's fares from other
    airports for nearly the same dates.
    """
    q = deal.quote
    band = wl.band_for(q.trip_length) if q.trip_length is not None else None
    if band is None:
        return None
    mine = ranked_total(deal, wl)
    for other in origins.values():
        if other.iata == q.origin or not _worth_it(other, q.destination):
            continue
        history = store.route_history(
            other.iata, q.destination, q.trip_length, band=band, before=as_of
        )
        if len(history) < ALT_MIN_HISTORY:
            continue
        usual = int(median(history))
        if ranking_cost_inr(usual, other.reach, wl.train_hour_value_inr) <= mine:
            return (
                f"flying from {place(other.iata)} usually costs less door to door "
                f"(about Rs {total_cost_inr(usual, other.reach):,})"
            )
    for alt in deals:
        a = alt.quote
        if (
            a.destination == q.destination
            and a.origin != q.origin
            and a.trip_length is not None
            and wl.band_for(a.trip_length) == band
            and abs((a.depart_date - q.depart_date).days) <= ALT_DATE_WINDOW_DAYS
            and ranked_total(alt, wl) < mine
        ):
            return (
                f"{place(a.origin)} is cheaper door to door for nearly the same dates "
                f"(Rs {alt.total_inr:,})"
            )
    return None


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
    tasks = [
        (o, d, m)
        for o in origins
        for d in destinations
        if d != o and _worth_it(origins[o], d)
        for m in months
    ]
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
                recorded += store.record(
                    usable_quotes(wl, provider.month_matrix(a, b, month), today)
                )
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

    far_months = months[NEAR_MONTHS:]
    far_budget = None
    if call_budget is not None:
        far_budget = int(max(0, call_budget - calls) * wl.far_month_share) if far_months else 0
    near_budget = None if call_budget is None else call_budget - far_budget

    dry = store.dry_routes(misses=DRY_AFTER_MISSES, recheck_days=DRY_RECHECK_DAYS, now=run_started)
    yields: dict[tuple[str, str], int] = {}  # usable round trips per route answered this run

    def price_round_trip(o: str, d: str, month: str) -> list[Quote]:
        errors_before = len(errors)
        quotes = attempt(f"cheap {o}->{d}", provider.cheap, o, d, month, month)
        if len(errors) == errors_before:
            usable = [q for q in usable_quotes(wl, quotes, today) if q.return_date]
            yields[(o, d)] = yields.get((o, d), 0) + len(usable)
        return quotes

    pairs, start, count = _wishlist_slice(store, wl, origins, near_budget, calls)
    chosen, near_cursor, skipped = _walk(pairs, start, count, lambda pair: pair in dry)
    for o, d in chosen:
        for month in months[:NEAR_MONTHS]:
            all_quotes += price_round_trip(o, d, month)
        # One-way in the *return* direction, to judge whether the way back is a
        # healthy price or a trap - see _return_leg_z.
        reverse = attempt(f"reverse {d}->{o}", provider.cheap, d, o, months[0])
        reverse_quotes.setdefault((o, d), []).extend(reverse)
        all_quotes += reverse
    # Same rule as seed: if nothing succeeded, those pairs were never priced.
    if pairs and (ok > 0 or not errors):
        store.set_meta(CURSOR_KEY, str(near_cursor))

    # Months beyond the near ones, often where booking-ahead fares sit, rotate
    # on their own cursor with a fixed share of the budget, so they can't starve
    # the near months and the near months can't starve them. Each month gets
    # its own reverse fetch, so its return leg is judged in the same month.
    far_items = [(o, d, m) for o, d in pairs for m in far_months]
    if far_items:
        if far_budget is None:
            far_count = len(far_items)
        else:
            far_count = min(len(far_items), far_budget // CALLS_PER_FAR_ITEM)
        far_start = int(store.get_meta(FAR_CURSOR_KEY, "0") or 0) % len(far_items)
        far_chosen, far_cursor, far_skipped = _walk(
            far_items, far_start, far_count, lambda item: item[:2] in dry
        )
        skipped += far_skipped
        ok_before, errors_before = ok, len(errors)
        for o, d, month in far_chosen:
            all_quotes += price_round_trip(o, d, month)
            reverse = attempt(f"reverse {d}->{o}", provider.cheap, d, o, month)
            reverse_quotes.setdefault((o, d), []).extend(reverse)
            all_quotes += reverse
        if far_chosen and (ok > ok_before or len(errors) == errors_before):
            store.set_meta(FAR_CURSOR_KEY, str(far_cursor))

    for (o, d), hits in yields.items():
        store.record_route_yield(o, d, hits, run_started)

    all_quotes = usable_quotes(wl, all_quotes, today)
    recorded = store.record(all_quotes)
    deals = score_quotes(
        store, wl, all_quotes, origins, reverse_quotes, marker=marker, as_of=run_started
    )

    to_alert = []
    a = wl.alerts
    lowest_routes: set[tuple[str, str]] = set()
    # Leads come cheapest first, so if several months of one route are all a
    # new low this run, the one lowest-fare alert sent is the cheapest of them.
    for deal in pick_leads(deals, wl):
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
        dry_skipped=skipped,
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
    """The alert as plain-text lines; Telegram gets the HTML form of the same text."""
    from .notify.message import render

    return render(deal, marker).splitlines()
