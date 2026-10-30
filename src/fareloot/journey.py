"""Door-to-door cost from Kerala, not just the airfare.

Flying out of Bangalore is often far cheaper than Kochi, but it costs a train
ticket and most of a day each way. Ranking on airfare alone would keep
recommending 14-hour rail journeys to save a few hundred rupees.
"""

from __future__ import annotations

from .models import Reach

# Rough block time: cruise speed plus taxi, climb and descent.
CRUISE_KMH = 750
FLIGHT_OVERHEAD_H = 0.5
# Any train up to this long is worth it for any flight; a day trip to TRV or
# CJB is ordinary. Past it, the train may take at most this many times as long
# as the flight: an overnight train to Bangalore for a 1.5h hop to Hyderabad
# is never a sensible trip, however cheap the hop.
DETOUR_FREE_HOURS = 5.0
DETOUR_MAX_RATIO = 3.0
# Trip lengths worth committing to, by flight distance. Shorter flights mean
# shorter trips; a long haul isn't worth three days, and three weeks is a big
# commitment anywhere.
SHORT_HAUL_KM = 1_500
LONG_HAUL_KM = 5_000
SHORT_TRIP = (3, 6)
MEDIUM_TRIP = (3, 10)
LONG_TRIP = (7, 14)

_MODE_LABELS = {
    "local": "local",
    "train": "train",
    "overnight_train": "overnight train",
    "bus": "bus",
}


def total_cost_inr(flight_inr: int, reach: Reach) -> int:
    """Real money: airfare plus getting to the airport and back."""
    return flight_inr + 2 * reach.cost_inr


def ranking_cost_inr(flight_inr: int, reach: Reach, train_hour_value_inr: float) -> float:
    """Total cost with travel time priced in, for ordering only.

    The time penalty is never shown as money — it exists so a 28-hour round trip
    on rail has to earn its place against flying from the local airport.
    """
    return total_cost_inr(flight_inr, reach) + 2 * reach.hours * train_hour_value_inr


def reach_score(reach: Reach, max_reach_hours: float) -> float:
    """1.0 for the doorstep airport, 0.0 for the furthest one allowed."""
    if max_reach_hours <= 0:
        return 1.0
    return 1.0 - min(reach.hours / max_reach_hours, 1.0)


def flight_hours(km: int) -> float:
    return km / CRUISE_KMH + FLIGHT_OVERHEAD_H


def worth_the_detour(reach: Reach, km: int | None) -> bool:
    """Whether getting to this airport makes sense for a flight this long.

    Unknown distance can't be judged, so it passes and the door-to-door
    comparison in the watcher decides.
    """
    if reach.mode == "local" or km is None or reach.hours <= DETOUR_FREE_HOURS:
        return True
    return reach.hours <= DETOUR_MAX_RATIO * flight_hours(km)


def trip_window(km: int | None) -> tuple[int, int]:
    """The trip lengths, in nights, worth alerting on for a flight this long."""
    if km is None:
        return MEDIUM_TRIP
    if km < SHORT_HAUL_KM:
        return SHORT_TRIP
    if km < LONG_HAUL_KM:
        return MEDIUM_TRIP
    return LONG_TRIP


def describe(reach: Reach) -> str:
    if reach.mode == "local":
        return "local"
    label = _MODE_LABELS.get(reach.mode, reach.mode)
    return f"{label} {reach.hours:g}h, Rs {reach.cost_inr:,} each way"
