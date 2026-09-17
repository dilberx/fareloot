from __future__ import annotations

from pathlib import Path

import typer

from .config import (
    DEFAULT_CACHE_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_WATCHLIST_YAML,
    Settings,
    load_watchlist,
)
from .models import Quote
from .providers.mock import MockProvider
from .providers.travelpayouts import TravelpayoutsProvider
from .store import Store
from .watcher import poll, score_quotes

app = typer.Typer(add_completion=False, help="Catch abnormally cheap round trips from Kerala.")


def _make_provider(name: str, settings: Settings, *, max_calls: int | None):
    if name == "mock":
        return MockProvider()
    if not settings.has_provider:
        typer.secho(
            "No Travelpayouts token found. Set FARELOOT_TP_TOKEN in .env, "
            "or pass --provider mock to try it without one.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    return TravelpayoutsProvider(
        settings.tp_token,
        marker=settings.tp_marker,
        cache_dir=DEFAULT_CACHE_DIR,
        max_calls=max_calls,
    )


@app.command()
def init(force: bool = typer.Option(False, help="Overwrite an existing watchlist.yaml")) -> None:
    """Create config/watchlist.yaml and the local database."""
    if DEFAULT_CONFIG_PATH.exists() and not force:
        typer.echo(f"{DEFAULT_CONFIG_PATH} already exists - pass --force to overwrite.")
    else:
        DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_CONFIG_PATH.write_text(DEFAULT_WATCHLIST_YAML, encoding="utf-8")
        typer.echo(f"Wrote {DEFAULT_CONFIG_PATH}. Edit it to match where you can actually travel.")
    with Store(DEFAULT_DB_PATH):
        pass
    typer.echo(f"Database ready at {DEFAULT_DB_PATH}.")
    if not Path(".env").exists():
        typer.echo("No .env yet - copy .env.example to .env when you're ready for real data.")


@app.command()
def seed(
    provider: str = typer.Option("travelpayouts", help="'travelpayouts' or 'mock'"),
    max_calls: int = typer.Option(300, help="Safety cap on API calls this run"),
) -> None:
    """Bootstrap baselines with a month's worth of prices per route, instantly.

    Seeds both directions: origin->destination for the main route baseline, and
    destination->origin so the return-leg health check has something to judge
    a live reverse quote against from the very first poll.
    """
    wl = load_watchlist()
    settings = Settings()
    p = _make_provider(provider, settings, max_calls=max_calls)
    with Store(DEFAULT_DB_PATH) as store:
        total = 0
        origins = wl.origin_map()
        months = _first_two_months()
        for origin in origins.values():
            for destination in wl.destinations:
                for month in months:
                    for a, b in ((origin.iata, destination), (destination, origin.iata)):
                        try:
                            quotes = p.month_matrix(a, b, month)
                        except Exception as exc:
                            typer.echo(f"  skip {a}->{b} {month}: {exc}")
                            continue
                        total += store.record(quotes)
        typer.echo(f"Seeded {total} observations across {len(origins)} origins.")


def _row_to_quote(row) -> Quote:
    from datetime import date as _date

    return Quote(
        origin=row["origin"],
        destination=row["destination"],
        depart_date=_date.fromisoformat(row["depart_date"]),
        return_date=_date.fromisoformat(row["return_date"]) if row["return_date"] else None,
        price_inr=row["price_inr"],
        stops=row["stops"],
        airline=row["airline"],
        distance_km=row["distance_km"],
    )


def _first_two_months() -> list[str]:
    from datetime import date

    today = date.today().replace(day=1)
    months = [today.strftime("%Y-%m")]
    year, month = today.year, today.month + 1
    if month > 12:
        month, year = 1, year + 1
    months.append(date(year, month, 1).strftime("%Y-%m"))
    return months


@app.command()
def poll_cmd(
    provider: str = typer.Option("travelpayouts", "--provider", help="'travelpayouts' or 'mock'"),
    notify: str = typer.Option("console", help="'console' or 'telegram'"),
    max_calls: int = typer.Option(300, help="Safety cap on API calls this run"),
    no_discovery: bool = typer.Option(False, help="Skip the broad discovery arm"),
) -> None:
    """Fetch fresh prices, score them, and alert on anything hot."""
    from .config import DEFAULT_DB_PATH

    wl = load_watchlist()
    settings = Settings()
    p = _make_provider(provider, settings, max_calls=max_calls)
    with Store(DEFAULT_DB_PATH) as store:
        result = poll(
            p, store, wl, marker=settings.tp_marker, include_discovery=not no_discovery
        )
        store.prune()

    for err in result.errors:
        typer.secho(f"warning: {err}", fg=typer.colors.YELLOW)

    typer.echo(
        f"Recorded {result.observations_recorded} observations, "
        f"{result.calls_made} API calls, {len(result.to_alert)} new alert(s)."
    )

    if notify == "console":
        from .notify.console import render

        render(result.deals, wl)
    elif notify == "telegram":
        if not settings.has_telegram:
            typer.secho(
                "Telegram is not configured - set the two FARELOOT_TELEGRAM_* vars.",
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        from .notify.telegram import send_deals

        send_deals(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            result.to_alert,
            settings.tp_marker,
        )
        typer.echo(f"Sent {len(result.to_alert)} alert(s) to Telegram.")
    else:
        typer.secho(f"Unknown --notify value: {notify}", fg=typer.colors.RED)
        raise typer.Exit(1)


app.command(name="poll")(poll_cmd)


@app.command()
def deals(
    provider: str = typer.Option(
        "travelpayouts", help="'travelpayouts' or 'mock' (mock also polls fresh data)"
    ),
    limit: int = typer.Option(30, help="How many rows to show"),
) -> None:
    """Show currently scored deals without sending any alerts."""
    wl = load_watchlist()
    settings = Settings()
    with Store(DEFAULT_DB_PATH) as store:
        if provider == "mock":
            p = MockProvider()
            result = poll(p, store, wl, marker=settings.tp_marker)
            all_deals = result.deals
        else:
            # Re-score what's already stored - no new API calls, so this is
            # free to run as often as you like.
            quotes = [_row_to_quote(row) for row in store.recent_quotes(limit=2000)]
            all_deals = score_quotes(
                store, wl, quotes, marker=settings.tp_marker, promote_discoveries=False
            )

    from .notify.console import render

    render(all_deals, wl, limit=limit)


@app.command()
def test_alert() -> None:
    """Send a single test message to confirm Telegram is wired up."""
    settings = Settings()
    if not settings.has_telegram:
        typer.secho(
            "Set FARELOOT_TELEGRAM_BOT_TOKEN and FARELOOT_TELEGRAM_CHAT_ID in .env first.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    from .notify.telegram import send_test

    send_test(settings.telegram_bot_token, settings.telegram_chat_id)
    typer.echo("Sent. Check Telegram.")


if __name__ == "__main__":
    app()
