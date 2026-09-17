"""Door-to-door cost from Kerala, not just the airfare.

Flying out of Bangalore is often far cheaper than Kochi, but it costs a train
ticket and most of a day each way. Ranking on airfare alone would keep
recommending 14-hour rail journeys to save a few hundred rupees.
"""

from __future__ import annotations

from .models import Reach

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


def describe(reach: Reach) -> str:
    if reach.mode == "local":
        return "local"
    label = _MODE_LABELS.get(reach.mode, reach.mode)
    return f"{label} {reach.hours:g}h, Rs {reach.cost_inr:,} each way"
