"""A deterministic fake provider.

Exists so the whole pipeline - poll, score, dedup, alert formatting - can be run
and tested before anyone signs up for an API token. Prices are generated from a
hash of the route so they are stable across runs, with two scripted situations:

  COK -> GYD   a genuine round-trip crash, which should reach LOOT
  COK -> TBS   an equally cheap-looking round trip whose return market is
               gouging, which must NOT reach a pushable tier
"""

from __future__ import annotations

import hashlib
import random
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta

from ..geo import km
from ..models import Quote

INR_PER_KM = 4.5
ROUND_TRIP_MULTIPLIER = 1.85
DAILY_SPREAD = 0.25

# route -> multiplier applied to the current quote
CRASHES: dict[tuple[str, str], float] = {
    ("COK", "GYD"): 0.45,
    ("COK", "TBS"): 0.47,
}

# routes whose *return* direction is abnormally expensive right now
RETURN_TRAPS: set[tuple[str, str]] = {("COK", "TBS")}
RETURN_TRAP_MULTIPLIER = 2.2

POPULAR = ["BKK", "KUL", "SIN", "CMB", "DXB", "GYD", "TBS", "ALA", "IST", "HKT", "MLE", "KTM"]


def _rng(*parts: object) -> random.Random:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _distance_km(origin: str, destination: str) -> int:
    # Real distances where known, so airports compare the way they would on real
    # fares: random ones made Madurai to Kuala Lumpur a third of Kochi's price.
    real = km(origin, destination)
    if real is not None:
        return max(400, real)
    pair = "".join(sorted([origin, destination]))
    return 400 + _rng("dist", pair).randrange(0, 7600)


def _base_one_way(origin: str, destination: str) -> float:
    km = _distance_km(origin, destination)
    route_bias = 0.8 + _rng("bias", origin, destination).random() * 0.5
    return km * INR_PER_KM * route_bias


def _price(
    origin: str, destination: str, depart: date, trip_length: int | None, day_key: object
) -> int:
    base = _base_one_way(origin, destination)
    if trip_length is not None:
        base *= ROUND_TRIP_MULTIPLIER
    spread = _rng("day", origin, destination, depart, trip_length, day_key).uniform(
        1 - DAILY_SPREAD, 1 + DAILY_SPREAD
    )
    return int(base * spread)


class MockProvider:
    name = "mock"

    def __init__(self, *, today: date | None = None):
        self.today = today or date.today()
        self.calls_made = 0

    # -- helpers ---------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _quote(
        self,
        origin: str,
        destination: str,
        depart: date,
        trip_length: int | None,
        *,
        day_key: object = "history",
        live: bool = False,
    ) -> Quote:
        price = _price(origin, destination, depart, trip_length, day_key)
        if live:
            price = int(price * CRASHES.get((origin, destination), 1.0))
            # This call is the return direction of a trapped route, e.g. TBS->COK
            # for the trap declared as COK->TBS.
            if (destination, origin) in RETURN_TRAPS:
                price = int(price * RETURN_TRAP_MULTIPLIER)
        return Quote(
            origin=origin,
            destination=destination,
            depart_date=depart,
            return_date=depart + timedelta(days=trip_length) if trip_length else None,
            price_inr=price,
            stops=_rng("stops", origin, destination, depart).randrange(0, 3),
            airline="MK",
            distance_km=_distance_km(origin, destination),
            observed_at=self._now(),
        )

    # -- Provider --------------------------------------------------------

    def cheap(
        self, origin: str, destination: str, depart: str, return_: str | None = None
    ) -> list[Quote]:
        self.calls_made += 1
        year, month = (int(p) for p in depart[:7].split("-"))
        first = date(year, month, 1)
        lengths: list[int | None] = [7, 10, 14] if return_ else [None]
        out = []
        for offset in (4, 11, 18, 25):
            depart_day = first + timedelta(days=offset)
            for length in lengths:
                out.append(
                    self._quote(
                        origin, destination, depart_day, length, day_key=self.today, live=True
                    )
                )
        return out

    def month_matrix(self, origin: str, destination: str, month: str) -> list[Quote]:
        """A calendar baseline, deliberately unaffected by any live crash.

        Includes a one-way entry (trip_length None) per day alongside the
        round-trip variants, so `seed` can build a clean reverse-direction
        one-way baseline too - that's what the return-leg health check in
        scoring compares a live reverse quote against.
        """
        self.calls_made += 1
        year, mon = (int(p) for p in month.split("-")[:2])
        days = monthrange(year, mon)[1]
        out = []
        for day in range(1, days + 1):
            depart = date(year, mon, day)
            if depart < self.today:
                continue
            for length in (None, 7, 10, 14):
                out.append(self._quote(origin, destination, depart, length))
        return out

    def city_directions(self, origin: str) -> list[Quote]:
        self.calls_made += 1
        out = []
        for i, destination in enumerate(POPULAR):
            depart = self.today + timedelta(days=30 + i * 3)
            out.append(self._quote(origin, destination, depart, 10, day_key=self.today, live=True))
        return out

    def latest(
        self, origin: str, *, one_way: bool = False, trip_duration: int | None = None
    ) -> list[Quote]:
        self.calls_made += 1
        length = None if one_way else (trip_duration or 10)
        out = []
        for i, destination in enumerate(POPULAR):
            depart = self.today + timedelta(days=21 + i * 5)
            out.append(
                self._quote(origin, destination, depart, length, day_key=self.today, live=True)
            )
        return out
