from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol, runtime_checkable

from ..models import Quote


@runtime_checkable
class Provider(Protocol):
    """A source of fare quotes.

    Every method returns already-normalised Quotes in INR, so nothing downstream
    needs to know which service the numbers came from.
    """

    name: str

    def cheap(
        self, origin: str, destination: str, depart: str, return_: str | None = None
    ) -> list[Quote]:
        """Cheapest itineraries for a route. `depart`/`return_` are YYYY-MM or YYYY-MM-DD."""
        ...

    def month_matrix(self, origin: str, destination: str, month: str) -> list[Quote]:
        """A price for every departure day in `month` (YYYY-MM). Used to seed baselines."""
        ...

    def city_directions(self, origin: str) -> list[Quote]:
        """Popular destinations from an origin, with indicative prices."""
        ...

    def latest(
        self, origin: str, *, one_way: bool = False, trip_duration: int | None = None
    ) -> list[Quote]:
        """Cheapest fares recently seen from an origin, across all destinations."""
        ...


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
