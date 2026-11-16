"""Check a candidate alert against a live price before it reaches your phone.

Travelpayouts prices come from a search cache and can be hours old, so an alert
can point at a fare that has already gone. Google Flights, through SerpApi,
returns live prices, but the free tier is about 250 searches a month: far too
few to scan with, enough to confirm the rare deals worth sending.

Any failure of the checker leaves an alert unconfirmed, never dropped. Only a
live price clearly above the cached one drops it.

A confirmed alert also gets a few other trip lengths priced live from the same
departure date. Measured on live data, Travelpayouts caches zero to three round
trips a year on most of these routes, so the cache alone can rarely offer a
second length; Google can price any. Those searches are optional and stop when
the month's usage runs ahead of an even pace.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import httpx

from .geo import km
from .journey import trip_menu
from .models import Deal, Quote, Tier
from .store import Store

URL = "https://serpapi.com/search"
STALE_TOLERANCE = 0.10
# Other trip lengths priced live for each confirmed alert; each is one search.
LIVE_LENGTHS_PER_ALERT = 2
NO_RESULTS = "hasn't returned any results"


class ConfirmError(RuntimeError):
    pass


@dataclass(frozen=True)
class LivePrice:
    price: int
    # Google's own verdict on this fare, from an entirely separate dataset.
    level: str = ""  # "low", "typical" or "high"
    typical: tuple[int, int] | None = None


class SerpApiFlights:
    """Live round-trip prices from Google Flights via SerpApi."""

    def __init__(self, api_key: str, *, client: httpx.Client | None = None):
        if not api_key:
            raise ConfirmError("no SerpApi key configured")
        self._key = api_key
        self._client = client or httpx.Client(timeout=40.0)

    def live_price(self, q: Quote) -> int | None:
        """Lowest live price for this exact itinerary, or None if nothing is listed."""
        live = self.live_quote(q)
        return live.price if live else None

    def live_quote(self, q: Quote) -> LivePrice | None:
        """Lowest live price plus Google's price insights, or None if nothing is listed."""
        params: dict[str, Any] = {
            "engine": "google_flights",
            "departure_id": q.origin,
            "arrival_id": q.destination,
            "outbound_date": q.depart_date.isoformat(),
            "currency": "INR",
            "hl": "en",
            "gl": "in",
            "adults": 1,
            "api_key": self._key,
        }
        if q.return_date:
            params.update(type=1, return_date=q.return_date.isoformat())
        else:
            params["type"] = 2
        try:
            resp = self._client.get(URL, params=params)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # The API key is a query parameter and httpx puts the request URL in
            # its exception text; Actions logs here are public.
            raise ConfirmError(f"live check request failed: {type(exc).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            raise ConfirmError(f"live check returned HTTP {resp.status_code}, not JSON") from None
        error = data.get("error") if isinstance(data, dict) else None
        if error:
            if NO_RESULTS in str(error):
                return None
            raise ConfirmError(f"live check failed: {str(error)[:160]}")
        if resp.status_code != 200:
            raise ConfirmError(f"live check returned HTTP {resp.status_code}")
        price = _lowest_price(data)
        if price is None:
            return None
        insights = data.get("price_insights") or {}
        level = insights.get("price_level")
        band = insights.get("typical_price_range")
        typical = None
        if (
            isinstance(band, list)
            and len(band) == 2
            and all(isinstance(x, int | float) for x in band)
        ):
            typical = (int(band[0]), int(band[1]))
        return LivePrice(price, level if isinstance(level, str) else "", typical)


def _lowest_price(data: dict[str, Any]) -> int | None:
    prices = [
        option.get("price")
        for key in ("best_flights", "other_flights")
        for option in data.get(key) or []
        if isinstance(option, dict)
    ]
    prices = [int(p) for p in prices if isinstance(p, int | float) and p > 0]
    if prices:
        return min(prices)
    insight = (data.get("price_insights") or {}).get("lowest_price")
    return int(insight) if isinstance(insight, int | float) and insight > 0 else None


@dataclass(frozen=True)
class Confirmation:
    status: str  # "confirmed", "stale", or "unconfirmed"
    live_price: int | None
    note: str
    google_level: str = ""
    google_typical: tuple[int, int] | None = None


def check(confirmer: SerpApiFlights, deal: Deal) -> Confirmation:
    cached = deal.quote.price_inr
    try:
        live = _live(confirmer, deal.quote)
    except ConfirmError as exc:
        return Confirmation("unconfirmed", None, str(exc))
    if live is None:
        return Confirmation("unconfirmed", None, "not listed on Google Flights")
    if live.price > cached * (1 + STALE_TOLERANCE):
        note = f"live price Rs {live.price:,} vs cached Rs {cached:,}"
        return Confirmation("stale", live.price, note)
    return Confirmation("confirmed", live.price, "", live.level, live.typical)


def _live(confirmer, q: Quote) -> LivePrice | None:
    # Confirmers that only report a price (older fakes, other sources) still work.
    if hasattr(confirmer, "live_quote"):
        return confirmer.live_quote(q)
    price = confirmer.live_price(q)
    return LivePrice(price) if price is not None else None


def _usage_key(now: datetime) -> str:
    return f"live_checks:{now:%Y-%m}"


def on_pace(used: int, monthly_cap: int, now: datetime) -> bool:
    """Whether optional searches may still be spent this month.

    They stop once usage is ahead of an even spend across the month, so they
    can never starve the checks that decide whether an alert is real.
    """
    days = monthrange(now.year, now.month)[1]
    return used < monthly_cap * now.day / days


def other_lengths(q: Quote) -> list[int]:
    """Trip lengths worth pricing next to this one, nearest first.

    The middle of every other length group the distance suits.
    """
    if q.trip_length is None:
        return []
    menu = trip_menu(km(q.origin, q.destination) or q.distance_km)
    mids = [(lo + hi) // 2 for lo, hi in menu if not lo <= q.trip_length <= hi]
    return sorted(mids, key=lambda n: abs(n - q.trip_length))


def _live_option(deal: Deal, q: Quote, live: LivePrice) -> Deal:
    return Deal(
        quote=replace(q, price_inr=live.price),
        origin=deal.origin,
        hotness=0,
        tier=Tier.NONE,
        baseline_inr=None,
        saving_inr=0,
        pct_below=0.0,
        one_way_only=False,
        reasons=[],
        live_price_inr=live.price,
    )


def _merge_live(deal: Deal, live: list[Deal]) -> None:
    """Live lengths replace cached options in the same length group."""
    q = deal.quote
    menu = trip_menu(km(q.origin, q.destination) or q.distance_km)

    def group(n: int) -> tuple[int, int] | int:
        return next(((lo, hi) for lo, hi in menu if lo <= n <= hi), n)

    taken = {group(o.quote.trip_length) for o in live}
    kept = [o for o in deal.options if group(o.quote.trip_length) not in taken]
    deal.options = sorted(kept + live, key=lambda o: o.quote.trip_length)


def confirm_alerts(
    deals: list[Deal],
    confirmer: SerpApiFlights | None,
    store: Store,
    *,
    monthly_cap: int,
    now: datetime,
    extra_lengths: int = 0,
) -> tuple[list[Deal], list[Deal]]:
    """Split alerts into (send, stale), annotating each deal with what was found.

    Searches are counted per calendar month so the free tier is never exceeded;
    once the cap is reached, alerts go out unconfirmed instead of being held.

    A stale lead hands over to its alternates in order, so one dead cache entry
    doesn't cost the route its alert. The first that isn't stale is sent, with
    the dates found gone removed from its options.

    With `extra_lengths`, a confirmed alert gets up to that many other trip
    lengths priced live from its departure date, while the month is on pace.
    A failed or empty extra search costs the alert nothing but that line.
    """
    send: list[Deal] = []
    stale: list[Deal] = []
    key = _usage_key(now)
    used = int(store.get_meta(key, "0") or 0)
    for lead in deals:
        gone: list[Deal] = []
        for deal in (lead, *lead.alternates):
            confirmed = False
            if confirmer is None:
                deal.live_note = "not live-checked: no SerpApi key configured"
            elif used >= monthly_cap:
                deal.live_note = (
                    f"not live-checked: monthly limit of {monthly_cap} searches reached"
                )
            else:
                result = check(confirmer, deal)
                used += 1
                store.set_meta(key, str(used))
                if result.status == "stale":
                    deal.live_price_inr = result.live_price
                    deal.live_note = result.note
                    gone.append(deal)
                    continue
                if result.status == "confirmed":
                    deal.live_price_inr = result.live_price
                    deal.google_level = result.google_level
                    deal.google_typical = result.google_typical
                    confirmed = True
                else:
                    deal.live_note = f"not live-checked: {result.note}"
            dead = {(g.quote.depart_date, g.quote.return_date) for g in gone}
            deal.options = [
                o for o in deal.options if (o.quote.depart_date, o.quote.return_date) not in dead
            ]
            send.append(deal)
            if confirmed:
                live: list[Deal] = []
                for nights in other_lengths(deal.quote)[:extra_lengths]:
                    if used >= monthly_cap or not on_pace(used, monthly_cap, now):
                        break
                    q = replace(
                        deal.quote, return_date=deal.quote.depart_date + timedelta(days=nights)
                    )
                    used += 1
                    store.set_meta(key, str(used))
                    try:
                        found = _live(confirmer, q)
                    except ConfirmError:
                        continue
                    if found is not None:
                        live.append(_live_option(deal, q, found))
                if live:
                    _merge_live(deal, live)
            break
        stale += gone
    return send, stale
