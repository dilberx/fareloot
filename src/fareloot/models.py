from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class Tier(str, Enum):
    JACKPOT = "JACKPOT"
    LOOT = "LOOT"
    STEAL = "STEAL"
    DEAL = "DEAL"
    NONE = "NONE"

    @property
    def rank(self) -> int:
        return _TIER_RANK[self]

    @property
    def pushes(self) -> bool:
        return self in (Tier.JACKPOT, Tier.LOOT, Tier.STEAL)


_TIER_RANK = {Tier.NONE: 0, Tier.DEAL: 1, Tier.STEAL: 2, Tier.LOOT: 3, Tier.JACKPOT: 4}


@dataclass(frozen=True)
class Reach:
    """What it costs to get from home to a departure airport."""

    mode: str
    hours: float
    cost_inr: int


@dataclass(frozen=True)
class Origin:
    iata: str
    name: str
    reach: Reach


@dataclass(frozen=True)
class Quote:
    """A priced itinerary as observed at a point in time.

    When return_date is set, price_inr is the round-trip total, not one leg.
    """

    origin: str
    destination: str
    depart_date: date
    price_inr: int
    return_date: date | None = None
    stops: int | None = None
    airline: str | None = None
    distance_km: int | None = None
    expires_at: datetime | None = None
    observed_at: datetime | None = None

    @property
    def is_round_trip(self) -> bool:
        return self.return_date is not None

    @property
    def trip_length(self) -> int | None:
        if self.return_date is None:
            return None
        return (self.return_date - self.depart_date).days

    @property
    def depart_month(self) -> str:
        return self.depart_date.strftime("%Y-%m")

    @property
    def inr_per_km(self) -> float | None:
        if not self.distance_km:
            return None
        # A round trip covers the distance twice; normalise so one-ways and
        # round trips land on the same scale.
        km = self.distance_km * (2 if self.is_round_trip else 1)
        return self.price_inr / km


@dataclass
class Deal:
    """A scored quote, ready to rank or alert on."""

    quote: Quote
    origin: Origin
    hotness: int
    tier: Tier
    baseline_inr: int | None
    saving_inr: int
    pct_below: float
    one_way_only: bool
    reasons: list[str]
    # Qualified by a lowest-fare rule: the route is at its cheapest in the rule's
    # window. Cooled down per route, not per departure date.
    lowest_in_window: bool = False

    @property
    def total_inr(self) -> int:
        """Flight plus the real round-trip cost of reaching the airport."""
        return self.quote.price_inr + 2 * self.origin.reach.cost_inr
