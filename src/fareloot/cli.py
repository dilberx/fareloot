from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import typer

from .config import (
    DEFAULT_CACHE_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_WATCHLIST_YAML,
    Settings,
    Watchlist,
    WatchlistError,
    load_watchlist,
)
from .models import Quote
from .providers.mock import MockProvider
from .providers.travelpayouts import TravelpayoutsProvider
from .store import Store
from .watcher import poll, record_alerts, score_quotes, summarize_errors
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
        market=settings.tp_market,
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
    destinations: str = typer.Option(
        "",
        help="Comma-separated destinations to seed, e.g. SHJ,RKT. Empty seeds them all.",
    ),
) -> None:
    """Bootstrap baselines from month-matrix calendars, in both directions."""
    wl = _load_watchlist()
    settings = Settings()
    p = _make_provider(provider, settings, max_calls=max_calls)
    with Store(DEFAULT_DB_PATH) as store:
        # The mock costs nothing, so seed it all at once for local demos.
        budget = None if provider == "mock" else int(max_calls * 0.9)
        only = [d.strip() for d in destinations.split(",") if d.strip()] or None
        try:
            r = seed_routes(p, store, wl, call_budget=budget, only=only)
        except ValueError as exc:
            _fail(str(exc))

    _report_errors(r.errors[:5] if not _in_ci() else r.errors)
    if not _in_ci() and len(r.errors) > 5:
        typer.secho(f"warning: ...and {len(r.errors) - 5} more", fg=typer.colors.YELLOW)
    if r.all_calls_failed:
        _fail("every provider call failed - nothing was seeded and the cursor did not move")

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


def _in_ci() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _fail(message: str) -> NoReturn:
    if _in_ci():
        typer.echo(f"::error::{message}")
    else:
        typer.secho(message, fg=typer.colors.RED)
    raise typer.Exit(1)


def _report_errors(errors: list[str]) -> None:
    """Print provider errors without revealing the watchlist in public logs.

    Each error names its route, and in CI those route pairs land in a public
    Actions log, so there they are grouped by cause instead.
    """
    if not errors:
        return
    if _in_ci():
        for cause, n in summarize_errors(errors):
            typer.echo(f"::warning::{n} provider call(s) failed: {cause}")
        return
    for err in errors:
        typer.secho(f"warning: {err}", fg=typer.colors.YELLOW)


def _load_watchlist() -> Watchlist:
    try:
        return load_watchlist()
    except WatchlistError as exc:
        _fail(str(exc))


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

    if notify not in ("console", "telegram"):
        typer.secho(f"Unknown --notify value: {notify}", fg=typer.colors.RED)
        raise typer.Exit(1)
    settings = Settings()
    if notify == "telegram" and not settings.has_telegram:
        # Checked before polling so a misconfigured run spends no API calls and
        # records nothing it cannot deliver.
        _fail("Telegram is not configured - set the two FARELOOT_TELEGRAM_* secrets.")

    wl = _load_watchlist()
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

    _report_errors(result.errors)
    fallbacks = getattr(p, "fallbacks", 0)
    if fallbacks:
        note = f"v3 prices_for_dates failed {fallbacks} time(s); used the deprecated v1 endpoint"
        typer.echo(f"::warning::{note}" if _in_ci() else f"warning: {note}")
    typer.echo(
        f"Recorded {result.observations_recorded} observations, "
        f"{result.calls_made} API calls, {len(result.to_alert)} new alert(s)."
    )
    if result.all_calls_failed:
        _fail("every provider call failed - check FARELOOT_TP_TOKEN and Travelpayouts status")

    if notify == "console":
        from .notify.console import render

        render(result.deals, wl)
        delivered = result.to_alert
    else:
        from .confirm import SerpApiFlights, confirm_alerts
        from .notify.telegram import TelegramError, send_deals

        to_send = result.to_alert
        if to_send:
            confirmer = SerpApiFlights(settings.serpapi_key) if settings.serpapi_key else None
            with Store(DEFAULT_DB_PATH) as store:
                to_send, stale = confirm_alerts(
                    result.to_alert,
                    confirmer,
                    store,
                    monthly_cap=settings.serpapi_monthly_cap,
                    now=datetime.now(UTC),
                )
                # A fare that is gone live is handled, not lost: cool it down so
                # the same stale cache entry doesn't spend a search every run.
                record_alerts(store, stale)
            confirmed = sum(1 for d in to_send if d.live_price_inr is not None)
            typer.echo(
                f"Live check: {confirmed} confirmed, {len(stale)} dropped as stale, "
                f"{len(to_send) - confirmed} sent unconfirmed."
            )
        try:
            delivered = send_deals(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                to_send,
                settings.tp_marker,
            )
        except TelegramError as exc:
            # Record what did reach Telegram; the rest stays eligible next run
            # instead of being hidden by the cooldown.
            with Store(DEFAULT_DB_PATH) as store:
                record_alerts(store, exc.delivered)
            _fail(f"{exc} ({len(exc.delivered)} of {len(to_send)} alerts delivered)")
        typer.echo(f"Sent {len(delivered)} alert(s) to Telegram.")

    with Store(DEFAULT_DB_PATH) as store:
        record_alerts(store, delivered)


app.command(name="poll")(poll_cmd)


@app.command()
def deals(
    provider: str = typer.Option(
        "travelpayouts", help="'travelpayouts' or 'mock' (mock also polls fresh data)"
    ),
    limit: int = typer.Option(30, help="How many rows to show"),
) -> None:
    """Show currently scored deals without sending any alerts."""
    wl = _load_watchlist()
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
def health(
    last_main_commit: str = typer.Option("", help="ISO time of the last commit to main"),
    last_watch_success: str = typer.Option("", help="ISO time of the last successful watch run"),
    watch_state: str = typer.Option("active", help="State of watch.yml from the Actions API"),
    notify: str = typer.Option("console", help="'console' or 'telegram'"),
) -> None:
    """Check the deployed watcher is alive and will stay alive; warn if not."""
    from datetime import UTC, datetime

    from .health import assess
    from .providers.base import parse_dt

    report = assess(
        datetime.now(UTC),
        parse_dt(last_main_commit),
        parse_dt(last_watch_success),
        watch_state,
    )
    if report.ok:
        typer.echo("fareloot is healthy.")
        return
    message = "fareloot needs attention:\n\n" + "\n\n".join(f"- {w}" for w in report.warnings)
    typer.echo(message)
    if notify == "telegram":
        settings = Settings()
        if not settings.has_telegram:
            _fail("health check failed and Telegram is not configured to report it")
        from .notify.telegram import _send

        _send(settings.telegram_bot_token, settings.telegram_chat_id, message)
        typer.echo("Sent the warning to Telegram.")
    # Red in the Actions tab too, for anyone who does look there.
    raise typer.Exit(1)


@app.command()
def provider_check(
    origin: str = typer.Option("COK"),
    destination: str = typer.Option("DXB"),
) -> None:
    """Price one route through each Travelpayouts endpoint and market, side by side.

    Spends 4 calls. Shows whether v3 prices_for_dates returns data at least as
    good as the deprecated v1 endpoint, and whether forcing the India market
    adds fares over letting the API infer one.
    """
    from datetime import date

    from .providers.travelpayouts import ProviderError

    settings = Settings()
    p = _make_provider("travelpayouts", settings, max_calls=10)
    today = date.today()
    month = date(today.year + (today.month == 12), today.month % 12 + 1, 1).strftime("%Y-%m")
    o, d = origin.upper(), destination.upper()
    checks = [
        ("v1 cheap, round trip", lambda: p.cheap_v1(o, d, month, month)),
        (
            "v3, round trip, inferred market",
            lambda: p.prices_for_dates(o, d, month, month, market=""),
        ),
        ("v3, round trip, market=in", lambda: p.prices_for_dates(o, d, month, month, market="in")),
        ("v3, one way back, inferred", lambda: p.prices_for_dates(d, o, month, market="")),
    ]
    typer.echo(f"{o}<->{d}, departures in {month}")
    for label, fetch in checks:
        try:
            quotes = fetch()
        except ProviderError as exc:
            typer.echo(f"  {label:34s} FAILED: {exc}")
            continue
        if not quotes:
            typer.echo(f"  {label:34s} 0 fares")
            continue
        lengths = sorted({q.trip_length for q in quotes if q.trip_length is not None})
        span = f", trip lengths {lengths[0]}-{lengths[-1]}d" if lengths else ""
        cheapest = min(q.price_inr for q in quotes)
        typer.echo(f"  {label:34s} {len(quotes):3d} fares, cheapest Rs {cheapest:,}{span}")


@app.command()
def live_check(
    origin: str = typer.Option("COK"),
    destination: str = typer.Option("DXB"),
    days_ahead: int = typer.Option(30, help="Departure this many days from today"),
    trip_days: int = typer.Option(7),
) -> None:
    """Spend two SerpApi searches pricing one route live, round trip and one way.

    Confirms the key works, and shows whether the round-trip figure is the total
    for both legs: it should sit well above a single one-way fare.
    """
    from datetime import date, timedelta

    from .confirm import ConfirmError, SerpApiFlights
    from .models import Quote

    settings = Settings()
    if not settings.serpapi_key:
        _fail("Set FARELOOT_SERPAPI_KEY first.")
    depart = date.today() + timedelta(days=days_ahead)
    rt = Quote(
        origin=origin.upper(),
        destination=destination.upper(),
        depart_date=depart,
        return_date=depart + timedelta(days=trip_days),
        price_inr=0,
    )
    ow = Quote(
        origin=origin.upper(), destination=destination.upper(), depart_date=depart, price_inr=0
    )
    flights = SerpApiFlights(settings.serpapi_key)
    try:
        rt_price, ow_price = flights.live_price(rt), flights.live_price(ow)
    except ConfirmError as exc:
        _fail(str(exc))
    typer.echo(f"{rt.origin}->{rt.destination} departing {depart}, {trip_days}-day trip")
    typer.echo(f"  round trip: {f'Rs {rt_price:,}' if rt_price else 'no results'}")
    typer.echo(f"  one way:    {f'Rs {ow_price:,}' if ow_price else 'no results'}")
    if rt_price and ow_price:
        ratio = rt_price / ow_price
        verdict = "both legs (total)" if ratio >= 1.3 else "possibly one leg only - check"
        typer.echo(
            f"  round trip / one way = {ratio:.2f} -> round-trip figure looks like {verdict}"
        )


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
