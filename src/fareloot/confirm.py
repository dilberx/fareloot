"""Check a candidate alert against a live price before it reaches your phone.

Travelpayouts prices come from a search cache and can be hours old, so an alert
can point at a fare that has already gone. Google Flights, through SerpApi,
returns live prices, but the free tier is about 250 searches a month: far too
few to scan with, enough to confirm the rare deals worth sending.

Any failure of the checker leaves an alert unconfirmed, never dropped. Only a
live price clearly above the cached one drops it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from .models import Deal, Quote
from .store import Store

URL = "https://serpapi.com/search"
STALE_TOLERANCE = 0.10
NO_RESULTS = "hasn't returned any results"


class ConfirmError(RuntimeError):
    pass


class SerpApiFlights:
    """Live round-trip prices from Google Flights via SerpApi."""

    def __init__(self, api_key: str, *, client: httpx.Client | None = None):
        if not api_key:
            raise ConfirmError("no SerpApi key configured")
        self._key = api_key
        self._client = client or httpx.Client(timeout=40.0)

    def live_price(self, q: Quote) -> int | None:
        """Lowest live price for this exact itinerary, or None if nothing is listed."""
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
        return _lowest_price(data)


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


def check(confirmer: SerpApiFlights, deal: Deal) -> Confirmation:
    cached = deal.quote.price_inr
    try:
        live = confirmer.live_price(deal.quote)
    except ConfirmError as exc:
        return Confirmation("unconfirmed", None, str(exc))
    if live is None:
        return Confirmation("unconfirmed", None, "not listed on Google Flights")
    if live > cached * (1 + STALE_TOLERANCE):
        return Confirmation("stale", live, f"live price Rs {live:,} vs cached Rs {cached:,}")
    return Confirmation("confirmed", live, "")


def _usage_key(now: datetime) -> str:
    return f"live_checks:{now:%Y-%m}"


def confirm_alerts(
    deals: list[Deal],
    confirmer: SerpApiFlights | None,
    store: Store,
    *,
    monthly_cap: int,
    now: datetime,
) -> tuple[list[Deal], list[Deal]]:
    """Split alerts into (send, stale), annotating each deal with what was found.

    Searches are counted per calendar month so the free tier is never exceeded;
    once the cap is reached, alerts go out unconfirmed instead of being held.
    """
    send: list[Deal] = []
    stale: list[Deal] = []
    key = _usage_key(now)
    used = int(store.get_meta(key, "0") or 0)
    for deal in deals:
        if confirmer is None:
            deal.live_note = "not live-checked: no SerpApi key configured"
            send.append(deal)
            continue
        if used >= monthly_cap:
            deal.live_note = f"not live-checked: monthly limit of {monthly_cap} searches reached"
            send.append(deal)
            continue
        result = check(confirmer, deal)
        used += 1
        store.set_meta(key, str(used))
        if result.status == "stale":
            deal.live_price_inr = result.live_price
            deal.live_note = result.note
            stale.append(deal)
        elif result.status == "confirmed":
            deal.live_price_inr = result.live_price
            send.append(deal)
        else:
            deal.live_note = f"not live-checked: {result.note}"
            send.append(deal)
    return send, stale
