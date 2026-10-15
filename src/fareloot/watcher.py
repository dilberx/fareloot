"""Poll providers, score what comes back, and decide what to alert on.

This is wiring, not logic: the judgement calls live in scoring.py and baseline.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .baseline import robust_z
from .config import Watchlist
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


def score_quotes(
    store: Store,
    wl: Watchlist,
    quotes: list[Quote],
    origins: dict[str, Origin] | None = None,
    reverse_quotes: dict[tuple[str, str], list[Quote]] | None = None,
    *,
    marker: str = "",
    promote_discoveries: bool = True,
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
        deals.append(_to_deal(quote, origin, scored, marker=marker))
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


def seed(
    provider: Provider,
    store: Store,
    wl: Watchlist,
    *,
    today: date | None = None,
    call_budget: int | None = None,
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
    tasks = [(o, d, m) for o in origins for d in destinations if d != o for m in months]
    if not tasks:
        return SeedResult(0, 0, 0, 0, 0, False, [])

    if call_budget is None:
        count = len(tasks)
    else:
        count = max(0, min(len(tasks), call_budget // CALLS_PER_SEED_TASK))
    start = int(store.get_meta(SEED_CURSOR_KEY, "0") or 0) % len(tasks)

    recorded = 0
    errors: list[str] = []
    for i in range(count):
        origin_iata, destination, month = tasks[(start + i) % len(tasks)]
        for a, b in ((origin_iata, destination), (destination, origin_iata)):
            try:
                recorded += store.record(usable_quotes(wl, provider.month_matrix(a, b, month)))
            except Exception as exc:
                errors.append(f"month_matrix {a}->{b} {month}: {exc}")
    store.set_meta(SEED_CURSOR_KEY, str((start + count) % len(tasks)))

    return SeedResult(
        recorded=recorded,
        calls_made=getattr(provider, "calls_made", 0),
        tasks_done=count,
        tasks_total=len(tasks),
        start=start,
        cycle_completed=start + count >= len(tasks),
        errors=errors,
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
    origins = wl.origin_map()
    months = _depart_candidates(today, wl.search_horizon_months)
    all_quotes: list[Quote] = []
    reverse_quotes: dict[tuple[str, str], list[Quote]] = {}
    errors: list[str] = []
    calls = 0

    # Discovery goes first: about two calls per origin buys coverage of every
    # destination, so it must never be starved by the wishlist spending the budget.
    if include_discovery:
        for origin in origins.values():
            calls += 2
            try:
                all_quotes += provider.city_directions(origin.iata)
            except Exception as exc:  # provider errors must not kill the whole run
                errors.append(f"city_directions {origin.iata}: {exc}")
            try:
                all_quotes += provider.latest(origin.iata, one_way=False, trip_duration=10)
            except Exception as exc:
                errors.append(f"latest {origin.iata}: {exc}")

    pairs, start, count = _wishlist_slice(store, wl, origins, call_budget, calls)
    for i in range(count):
        origin_iata, destination = pairs[(start + i) % len(pairs)]
        for month in months[:2]:
            try:
                all_quotes += provider.cheap(origin_iata, destination, month, month)
            except Exception as exc:
                errors.append(f"cheap {origin_iata}->{destination}: {exc}")
        # One-way in the *return* direction, to judge whether the way back is a
        # healthy price or a trap - see _return_leg_z.
        try:
            reverse = provider.cheap(destination, origin_iata, months[0])
            reverse_quotes.setdefault((origin_iata, destination), []).extend(reverse)
            all_quotes += reverse
        except Exception as exc:
            errors.append(f"reverse {destination}->{origin_iata}: {exc}")
    if pairs:
        store.set_meta(CURSOR_KEY, str((start + count) % len(pairs)))

    all_quotes = usable_quotes(wl, all_quotes)
    recorded = store.record(all_quotes)
    deals = score_quotes(store, wl, all_quotes, origins, reverse_quotes, marker=marker)

    to_alert = []
    a = wl.alerts
    for deal in deals:
        if not deal.tier.pushes:
            continue
        if store.should_alert(
            deal.quote.origin,
            deal.quote.destination,
            deal.quote.trip_length,
            deal.quote.depart_date.isoformat(),
            deal.quote.price_inr,
            deal.tier,
            cooldown_hours=a.cooldown_hours,
            redrop_pct=a.redrop_pct,
        ):
            to_alert.append(deal)

    for deal in to_alert:
        store.record_alert(
            deal.quote.origin,
            deal.quote.destination,
            deal.quote.trip_length,
            deal.quote.depart_date.isoformat(),
            deal.quote.price_inr,
            deal.tier,
        )

    calls_made = getattr(provider, "calls_made", 0)
    return PollResult(
        deals=deals,
        to_alert=to_alert,
        observations_recorded=recorded,
        calls_made=calls_made,
        errors=errors,
    )


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
    for reason in deal.reasons:
        lines.append(f"  {reason}")
    lines.append(f"  via {aviasales(q, marker)}")
    lines.append(f"  or {google_flights(q)}")
    return lines
