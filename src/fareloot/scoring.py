"""Turn a price into a 0-100 hotness score and a tier.

Two rules shape everything here:

1. A steal is relative. The same rupee figure is a jackpot on one route and a
   ripoff on another, so every judgement is made against that route's own history.
2. A cheap outbound with a gouging return is not a deal. Nothing reaches a
   pushable tier unless the way back is also healthy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .baseline import median, percentile_rank, robust_z
from .journey import reach_score
from .models import Reach, Tier

# Component weights. Any component whose inputs are missing is dropped and the
# rest are renormalised, so an unknown never quietly contributes free points.
WEIGHTS = {
    "depth": 0.40,
    "rarity": 0.20,
    "value": 0.20,
    "absolute": 0.10,
    "reach": 0.10,
}

DEPTH_Z_FLOOR = 1.0  # z at which depth starts counting
DEPTH_Z_CEIL = 4.0  # z at which depth is maxed out
ABSOLUTE_SATURATION_INR = 15_000
# A return leg counts as a trap above this robust z. Measured on 1,680 return
# checks: 1.0 flagged 14.8% of healthy returns (about one genuine deal in seven
# blocked) while 2.0 flags 0.3% and still catches every planted trap (z >= +3.4).
RETURN_UNHEALTHY_Z = 2.0
CONFIDENCE_FULL_OBS = 20


@dataclass(frozen=True)
class Thresholds:
    jackpot: int = 95
    loot: int = 80
    steal: int = 65
    deal: int = 50
    min_saving_inr: int = 3_000
    min_pct_below_baseline: float = 25.0


@dataclass
class Scored:
    hotness: int
    tier: Tier
    baseline_inr: int | None
    saving_inr: int
    pct_below: float
    one_way_only: bool
    components: dict[str, float] = field(default_factory=dict)
    gates: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _depth(rt_z: float | None) -> float | None:
    if rt_z is None:
        return None
    return _clamp((-rt_z - DEPTH_Z_FLOOR) / (DEPTH_Z_CEIL - DEPTH_Z_FLOOR))


def _staleness(expires_at: datetime | None, now: datetime) -> float:
    if expires_at is None:
        return 1.0
    hours_left = (expires_at - now).total_seconds() / 3600.0
    if hours_left <= 0:
        return 0.0
    if hours_left >= 48:
        return 1.0
    return 0.7 + 0.3 * (hours_left / 48.0)


def _tier_for(hotness: int, t: Thresholds) -> Tier:
    if hotness >= t.jackpot:
        return Tier.JACKPOT
    if hotness >= t.loot:
        return Tier.LOOT
    if hotness >= t.steal:
        return Tier.STEAL
    if hotness >= t.deal:
        return Tier.DEAL
    return Tier.NONE


def _cap(tier: Tier, ceiling: Tier) -> Tier:
    return ceiling if tier.rank > ceiling.rank else tier


def score(
    *,
    price_inr: int,
    route_history: Sequence[float],
    reach: Reach,
    max_reach_hours: float,
    is_round_trip: bool,
    return_z: float | None = None,
    inr_per_km: float | None = None,
    global_inr_per_km: Sequence[float] = (),
    expires_at: datetime | None = None,
    now: datetime | None = None,
    thresholds: Thresholds | None = None,
) -> Scored:
    t = thresholds or Thresholds()
    now = now or datetime.now(UTC)

    rt_z = robust_z(price_inr, route_history)
    has_baseline = rt_z is not None
    baseline = int(median(route_history)) if route_history else None
    saving = max(0, baseline - price_inr) if baseline else 0
    pct_below = (saving / baseline * 100.0) if baseline else 0.0

    components: dict[str, float] = {}
    reasons: list[str] = []

    depth = _depth(rt_z)
    if depth is not None:
        components["depth"] = depth

    if has_baseline:
        components["rarity"] = 1.0 - percentile_rank(price_inr, route_history)
        components["absolute"] = _clamp(saving / ABSOLUTE_SATURATION_INR)

    if inr_per_km is not None and len(global_inr_per_km) >= 8:
        components["value"] = 1.0 - percentile_rank(inr_per_km, global_inr_per_km)

    components["reach"] = reach_score(reach, max_reach_hours)

    # Reach alone says nothing about whether a price is good.
    if "depth" not in components and "value" not in components:
        return Scored(
            hotness=0,
            tier=Tier.NONE,
            baseline_inr=baseline,
            saving_inr=saving,
            pct_below=pct_below,
            one_way_only=not is_round_trip,
            components=components,
            reasons=["not enough history to judge this route yet"],
        )

    available = sum(WEIGHTS[k] for k in components)
    raw = sum(components[k] * WEIGHTS[k] for k in components) / available

    one_way_only = not is_round_trip
    ceiling = Tier.JACKPOT
    gates: dict[str, float] = {}

    if one_way_only:
        gates["return_health"] = 0.7
        ceiling = Tier.DEAL
        reasons.append("one way only - no priced return, so it will not push")
    elif return_z is not None and return_z > RETURN_UNHEALTHY_Z:
        gates["return_health"] = 0.5
        ceiling = Tier.DEAL
        reasons.append(f"return leg is abnormally expensive (z {return_z:+.1f}) - held back")
    elif return_z is not None:
        gates["return_health"] = 1.0
        reasons.append(f"return leg normal (z {return_z:+.1f})")
    else:
        gates["return_health"] = 0.85

    gates["confidence"] = _clamp(len(route_history) / CONFIDENCE_FULL_OBS, 0.5, 1.0)
    gates["staleness"] = _staleness(expires_at, now)

    gate_product = gates["return_health"] * gates["confidence"] * gates["staleness"]
    hotness = int(round(100 * raw * gate_product))
    hotness = max(0, min(100, hotness))
    tier = _cap(_tier_for(hotness, t), ceiling)

    if has_baseline:
        if saving < t.min_saving_inr:
            tier = Tier.NONE
            reasons.append(f"saving Rs {saving:,} is under the Rs {t.min_saving_inr:,} floor")
        elif pct_below < t.min_pct_below_baseline:
            tier = Tier.NONE
            reasons.append(f"only {pct_below:.0f}% below normal, under the floor")
        else:
            reasons.insert(0, f"usually Rs {baseline:,} ({pct_below:.0f}% below)")
    else:
        # Discovery arm: no history, so this needs a confirming pass before it
        # is allowed to interrupt anyone.
        tier = _cap(tier, Tier.DEAL)
        reasons.append("new route - needs a confirming pass before it can push")

    if gates["staleness"] == 0.0:
        tier = Tier.NONE
        reasons.append("quote already expired")

    return Scored(
        hotness=hotness,
        tier=tier,
        baseline_inr=baseline,
        saving_inr=saving,
        pct_below=pct_below,
        one_way_only=one_way_only,
        components=components,
        gates=gates,
        reasons=reasons,
    )
