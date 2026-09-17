"""Booking hand-off.

Alerts link to a live search rather than a stored price. The cached quote is a
few hours old at best, so sending someone to a page that re-prices the route is
more honest than implying the number is bookable.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from .models import Quote


def aviasales(q: Quote, marker: str = "") -> str:
    leg = f"{q.origin}{q.depart_date:%d%m}{q.destination}"
    if q.return_date:
        leg += f"{q.return_date:%d%m}"
    url = f"https://www.aviasales.com/search/{leg}1"
    return f"{url}?marker={marker}" if marker else url


def google_flights(q: Quote) -> str:
    terms = f"Flights from {q.origin} to {q.destination} on {q.depart_date:%Y-%m-%d}"
    if q.return_date:
        terms += f" through {q.return_date:%Y-%m-%d}"
    return f"https://www.google.com/travel/flights?q={quote_plus(terms)}"
