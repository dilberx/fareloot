from __future__ import annotations

from rich.console import Console
from rich.table import Table

from ..config import Watchlist
from ..models import Deal
from ..watcher import ranked_total


def render(deals: list[Deal], wl: Watchlist, *, limit: int = 30) -> None:
    console = Console()
    if not deals:
        console.print("[yellow]No quotes scored yet - run `fareloot seed` first.[/yellow]")
        return

    table = Table(title="fareloot")
    table.add_column("Tier")
    table.add_column("Hot", justify="right")
    table.add_column("Route")
    table.add_column("Price", justify="right")
    table.add_column("Usually", justify="right")
    table.add_column("Reach")
    table.add_column("Total*", justify="right")

    colours = {
        "JACKPOT": "bold magenta",
        "LOOT": "bold red",
        "STEAL": "yellow",
        "DEAL": "cyan",
        "NONE": "dim",
    }

    for deal in deals[:limit]:
        q = deal.quote
        route = f"{q.origin}->{q.destination}"
        if q.is_round_trip:
            route += f"  ({q.trip_length}d)"
        price = f"Rs {q.price_inr:,}" + ("" if q.is_round_trip else " (1-way)")
        baseline = f"Rs {deal.baseline_inr:,}" if deal.baseline_inr else "-"
        colour = colours.get(deal.tier.value, "white")
        table.add_row(
            f"[{colour}]{deal.tier.value}[/{colour}]",
            str(deal.hotness),
            route,
            price,
            baseline,
            deal.origin.name,
            f"Rs {int(ranked_total(deal, wl)):,}",
        )
    console.print(table)
    console.print("[dim]*Total includes estimated round-trip cost of reaching the airport.[/dim]")
