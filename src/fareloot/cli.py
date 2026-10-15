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
from .watcher import seed as seed_routes

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
    max_calls: int = typer.Option(
        140,
        help="API calls this run, kept under the provider's 150/hour limiter. Each run "
        "resumes where the last one stopped; run it again until it reports a full cycle.",
    ),
) -> None:
    """Bootstrap baselines from month-matrix calendars, in both directions."""
    wl = load_watchlist()
    settings = Settings()
    p = _make_provider(provider, settings, max_calls=max_calls)
    with Store(DEFAULT_DB_PATH) as store:
        # The mock costs nothing, so seed it all at once for local demos.
        budget = None if provider == "mock" else int(max_calls * 0.9)
        r = seed_routes(p, store, wl, call_budget=budget)

    for err in r.errors[:5]:
        typer.secho(f"warning: {err}", fg=typer.colors.YELLOW)
    if len(r.errors) > 5:
        typer.secho(f"warning: ...and {len(r.errors) - 5} more", fg=typer.colors.YELLOW)

    end = r.start + r.tasks_done
    typer.echo(
        f"Seeded {r.recorded} observations from {r.calls_made} API calls "
        f"({seed_range_label(r.start, r.tasks_done, r.tasks_total)})."
    )
    if r.cycle_completed:
        typer.echo("Every route has now been seeded at least once.")
    else:
        remaining = r.tasks_total - end
        runs = -(-remaining // max(r.tasks_done, 1))
        typer.echo(f"{remaining} tasks left, about {runs} more run(s) at this budget.")


def seed_range_label(start: int, done: int, total: int) -> str:
    """Describe which seed tasks a run covered, splitting the range when it wraps."""
    end = start + done
    if end <= total:
        return f"tasks {start}-{end} of {total}"
    return f"tasks {start}-{total} and 0-{end - total} of {total}"


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


@app.command()
def poll_cmd(
    provider: str = typer.Option("travelpayouts", "--provider", help="'travelpayouts' or 'mock'"),
    notify: str = typer.Option("console", help="'console' or 'telegram'"),
    max_calls: int = typer.Option(
        140,
        help="API calls this run. Kept under the provider's 150/hour limiter so a run never "
        "sleeps; the wishlist resumes where the last run stopped.",
    ),
    no_discovery: bool = typer.Option(False, help="Skip the broad discovery arm"),
) -> None:
    """Fetch fresh prices, score them, and alert on anything hot."""
    from .config import DEFAULT_DB_PATH

    wl = load_watchlist()
    settings = Settings()
    p = _make_provider(provider, settings, max_calls=max_calls)
    with Store(DEFAULT_DB_PATH) as store:
        result = poll(
            p,
            store,
            wl,
            marker=settings.tp_marker,
            include_discovery=not no_discovery,
            # Plan below the hard cap so retries on flaky calls don't exhaust it
            # and drop the last few pairs of the slice.
            call_budget=int(max_calls * 0.9),
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
