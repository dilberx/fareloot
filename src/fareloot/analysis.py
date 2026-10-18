"""Where does the price history actually come from, and which routes are worth polling?

Produces two reports from the same numbers. The public one goes to the Actions
log and never names a destination, because destinations reveal the private
watchlist. The private one names everything and is meant for Telegram.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .config import Watchlist

SCOREABLE = 4  # baseline.MIN_OBS: below this a route band can't be scored at all
CONFIDENT = 20  # scoring reaches full confidence at this many observations


@dataclass
class DestinationYield:
    code: str
    round_trips: int = 0  # in-band, from any configured origin
    one_ways_out: int = 0
    one_ways_back: int = 0
    scoreable_bands: int = 0
    confident_bands: int = 0
    span_days: float = 0.0


@dataclass
class Analysis:
    total: int
    round_trips: int
    one_ways: int
    first_seen: str
    last_seen: str
    per_origin: dict[str, tuple[int, int, int]]  # round trips, one-ways out, destinations
    wishlist: list[DestinationYield]
    discovery_only: list[tuple[str, int]]
    rule_routes_ready: int
    rule_routes_total: int
    notes: list[str] = field(default_factory=list)


def analyse(conn: sqlite3.Connection, wl: Watchlist) -> Analysis:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT origin, destination, trip_length, price_inr, observed_at FROM observations"
    ).fetchall()
    origins = set(wl.origin_map())
    wishlist = {d.upper(): DestinationYield(d.upper()) for d in wl.destinations}
    per_origin: dict[str, list] = {o: [0, 0, set()] for o in origins}
    groups: dict[tuple[str, str, tuple[int, int]], list[str]] = defaultdict(list)
    discovery: dict[str, int] = defaultdict(int)
    seen = [r["observed_at"] for r in rows]

    rt_total = 0
    for r in rows:
        o, d, length = r["origin"], r["destination"], r["trip_length"]
        if length is not None:
            band = wl.band_for(length)
            if band is None:
                continue
            rt_total += 1
            if o in origins:
                per_origin[o][0] += 1
                per_origin[o][2].add(d)
                if d in wishlist:
                    wishlist[d].round_trips += 1
                    groups[(o, d, band)].append(r["observed_at"])
                else:
                    discovery[d] += 1
        else:
            if o in origins:
                per_origin[o][1] += 1
                if d in wishlist:
                    wishlist[d].one_ways_out += 1
            elif d in origins and o in wishlist:
                wishlist[o].one_ways_back += 1

    for (_, d, _), stamps in groups.items():
        dy = wishlist[d]
        if len(stamps) >= SCOREABLE:
            dy.scoreable_bands += 1
        if len(stamps) >= CONFIDENT:
            dy.confident_bands += 1
        span = (_ts(max(stamps)) - _ts(min(stamps))).total_seconds() / 86_400
        dy.span_days = max(dy.span_days, span)

    ready = total = 0
    for rule in wl.lowest_fare_rules:
        for d in rule.destinations:
            for o in origins:
                for band in wl.trip_bands:
                    total += 1
                    stamps = groups.get((o, d, tuple(band)), [])
                    if len(stamps) >= rule.min_history:
                        span = (_ts(max(stamps)) - _ts(min(stamps))).total_seconds() / 86_400
                        ready += span >= rule.min_history_days

    return Analysis(
        total=len(rows),
        round_trips=rt_total,
        one_ways=sum(1 for r in rows if r["trip_length"] is None),
        first_seen=min(seen)[:16] if seen else "-",
        last_seen=max(seen)[:16] if seen else "-",
        per_origin={o: (v[0], v[1], len(v[2])) for o, v in sorted(per_origin.items())},
        wishlist=sorted(wishlist.values(), key=lambda y: -y.round_trips),
        discovery_only=sorted(discovery.items(), key=lambda kv: -kv[1]),
        rule_routes_ready=ready,
        rule_routes_total=total,
    )


def _ts(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _bucket(n: int) -> str:
    if n == 0:
        return "0"
    if n < SCOREABLE:
        return f"1-{SCOREABLE - 1}"
    if n < CONFIDENT:
        return f"{SCOREABLE}-{CONFIDENT - 1}"
    return f"{CONFIDENT}+"


def public_report(a: Analysis) -> str:
    """Safe for a public log: origins (the published defaults) and counts only."""
    lines = [
        f"{a.total} observations, {a.first_seen} to {a.last_seen} UTC",
        f"  {a.round_trips} usable round trips, {a.one_ways} one-ways",
        "Per origin: usable round trips / one-ways out / distinct destinations",
    ]
    lines += [f"  {o}: {rt} / {ow} / {nd}" for o, (rt, ow, nd) in a.per_origin.items()]
    buckets: dict[str, int] = defaultdict(int)
    for y in a.wishlist:
        buckets[_bucket(y.round_trips)] += 1
    order = ["0", f"1-{SCOREABLE - 1}", f"{SCOREABLE}-{CONFIDENT - 1}", f"{CONFIDENT}+"]
    lines.append(
        f"Wishlist destinations by usable round trips ({len(a.wishlist)} total): "
        + ", ".join(f"{k}: {buckets.get(k, 0)}" for k in order)
    )
    lines.append(f"Destinations seen only via discovery: {len(a.discovery_only)}")
    if a.rule_routes_total:
        lines.append(
            f"Lowest-fare rule route-bands with enough history: "
            f"{a.rule_routes_ready} of {a.rule_routes_total}"
        )
    return "\n".join(lines)


def private_report(a: Analysis) -> str:
    """Names every destination. For Telegram only, never a public log."""
    lines = [
        "fareloot history, per wishlist destination",
        "(round trips | one-ways out/back | scoreable bands | span)",
    ]
    for y in a.wishlist:
        flag = "  <- nothing yet" if y.round_trips == 0 and y.one_ways_out == 0 else ""
        lines.append(
            f"{y.code}: {y.round_trips} | {y.one_ways_out}/{y.one_ways_back} | "
            f"{y.scoreable_bands} | {y.span_days:.1f}d{flag}"
        )
    if a.discovery_only:
        top = ", ".join(f"{d} {n}" for d, n in a.discovery_only[:12])
        lines.append(f"\nTop discovery-only destinations: {top}")
    return "\n".join(lines)
