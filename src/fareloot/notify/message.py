"""Alert text a newcomer can act on.

Places by name, the price you'd actually pay first, what the scoring found in
plain words, the train leg and door-to-door cost when the flight leaves from
another city, and labelled booking links. No hotness scores or z-values: those
are the scoring system talking to itself.

One renderer for both forms: HTML for Telegram (labelled links), plain text for
everywhere else.
"""

from __future__ import annotations

from html import escape

from ..links import aviasales, google_flights
from ..models import Deal, Tier
from ..places import place

TIER_WORDS = {
    Tier.JACKPOT: "a rare low",
    Tier.LOOT: "a big drop",
    Tier.STEAL: "a good drop",
    Tier.DEAL: "a small drop",
}
TRAIN_MODES = {"train": "train", "overnight_train": "overnight train", "bus": "bus"}


def _price(deal: Deal) -> int:
    return deal.live_price_inr if deal.live_price_inr is not None else deal.quote.price_inr


def _kind(deal: Deal) -> str:
    return "return" if deal.quote.is_round_trip else "one way"


def _dates(deal: Deal) -> str:
    q = deal.quote
    if q.return_date is None:
        return f"Departs {q.depart_date:%a %d %b}"
    return f"Out {q.depart_date:%a %d %b}, back {q.return_date:%a %d %b} ({q.trip_length} nights)"


def _links(deal: Deal, marker: str, html: bool) -> str:
    g, a = google_flights(deal.quote), aviasales(deal.quote, marker)
    if html:
        return (
            f'<a href="{escape(g, quote=True)}">Book on Google Flights</a> | '
            f'<a href="{escape(a, quote=True)}">Compare on Aviasales</a>'
        )
    return f"Book on Google Flights: {g}\nCompare on Aviasales: {a}"


def _price_line(deal: Deal) -> str:
    price = f"Rs {_price(deal):,} per person, {_kind(deal)}"
    if deal.live_price_inr is not None:
        return f"{price}, checked live on Google Flights just now"
    why = deal.live_note.removeprefix("not live-checked: ").strip()
    why = f" ({why})" if why else ""
    return (
        f"{price}. Seen in the last few hours and not re-checked live{why}, so it may have changed"
    )


def render(deal: Deal, marker: str = "", *, html: bool = False) -> str:
    q = deal.quote
    esc = escape if html else (lambda s: s)
    origin, dest = place(q.origin), place(q.destination)
    headline = f"{deal.tier.value}: {origin} to {dest}, Rs {_price(deal):,} {_kind(deal)}"
    lines = [f"<b>{esc(headline)}</b>" if html else headline]

    if deal.lowest_in_window:
        lines.append(
            f"The cheapest {origin} to {dest} {_kind(deal)} in {deal.lowest_window_days} days"
        )
    if deal.baseline_inr:
        lines.append(
            f"{deal.pct_below:.0f}% below the usual Rs {deal.baseline_inr:,} for this route, "
            f"{TIER_WORDS.get(deal.tier, 'a drop')}"
        )
    lines.append("")
    lines.append(_dates(deal))
    lines.append(_price_line(deal))

    reach = deal.origin.reach
    if reach.mode in TRAIN_MODES:
        lines.append(
            f"Getting to {origin}: {TRAIN_MODES[reach.mode]}, about {reach.hours:g}h, "
            f"Rs {reach.cost_inr:,} each way"
        )
        lines.append(f"Door to door: about Rs {_price(deal) + 2 * reach.cost_inr:,}")

    if deal.return_status == "healthy":
        lines.append("The return flight is normally priced, not a trap")
    elif q.is_round_trip:
        lines.append("The return flight's price wasn't checked separately")

    if deal.google_level or deal.google_typical:
        verdict = (
            f"Google also rates this fare {deal.google_level}" if deal.google_level else "Google"
        )
        if deal.google_typical:
            lo, hi = deal.google_typical
            verdict += f" (usually Rs {lo:,} to {hi:,})"
        lines.append(verdict)

    body = [esc(line) for line in lines[1:]]
    out = [lines[0], *body, "", _links(deal, marker, html)]
    if deal.tier in (Tier.JACKPOT, Tier.LOOT):
        out += ["", esc("Fares this cheap often disappear within hours.")]
    return "\n".join(out)


def render_short(deal: Deal, marker: str = "", *, html: bool = False) -> str:
    """One compact entry for a batched message of several deals."""
    q = deal.quote
    esc = escape if html else (lambda s: s)
    origin, dest = place(q.origin), place(q.destination)
    head = f"{origin} to {dest}, Rs {_price(deal):,} {_kind(deal)}"
    if deal.baseline_inr:
        head += f", {deal.pct_below:.0f}% below usual"
    when = _dates(deal)
    if deal.origin.reach.mode in TRAIN_MODES:
        when += f". {TRAIN_MODES[deal.origin.reach.mode].capitalize()} to {origin}"
    if deal.live_price_inr is None:
        when += ". Not re-checked live"
    else:
        when += ". Price checked live on Google Flights"
    first = f"<b>{esc(head)}</b>" if html else head
    return "\n".join([first, esc(when), _links(deal, marker, html)])
