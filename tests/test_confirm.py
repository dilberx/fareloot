"""Live check: confirm cached alerts against Google Flights before sending.

A checker failure must never cost a real deal (unconfirmed, still sent); only a
live price clearly above the cached one drops an alert.
"""

from datetime import UTC, date, datetime

import httpx
import pytest

from fareloot.confirm import ConfirmError, SerpApiFlights, check, confirm_alerts
from fareloot.models import Deal, Origin, Quote, Reach, Tier
from fareloot.store import Store
from fareloot.watcher import format_alert_lines

KEY = "serpapi-secret-key-value"
NOW = datetime(2026, 11, 15, tzinfo=UTC)


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.calls = payload, status, []

    def get(self, url, params=None):
        self.calls.append(params)
        if isinstance(self.payload, BaseException) and not isinstance(self.payload, ValueError):
            raise self.payload
        return _Resp(self.payload, self.status)


def _deal(price=7000):
    q = Quote(
        origin="COK",
        destination="DXB",
        depart_date=date(2026, 12, 10),
        return_date=date(2026, 12, 17),
        price_inr=price,
    )
    o = Origin(iata="COK", name="Kochi", reach=Reach(mode="local", hours=1, cost_inr=200))
    return Deal(
        quote=q,
        origin=o,
        hotness=85,
        tier=Tier.LOOT,
        baseline_inr=18000,
        saving_inr=18000 - price,
        pct_below=60.0,
        one_way_only=False,
        reasons=["x"],
    )


def _flights(payload, status=200):
    client = _Client(payload, status)
    return SerpApiFlights(KEY, client=client), client


GOOD = {
    "best_flights": [{"price": 7400}, {"price": 8100}],
    "other_flights": [{"price": 7250}, {"price": "n/a"}],
    "price_insights": {"lowest_price": 7000},
}


def test_lowest_price_across_best_and_other_flights():
    f, client = _flights(GOOD)
    assert f.live_price(_deal().quote) == 7250
    p = client.calls[0]
    assert p["engine"] == "google_flights" and p["type"] == 1 and p["currency"] == "INR"
    assert p["return_date"] == "2026-12-17"


def test_falls_back_to_price_insights():
    f, _ = _flights({"best_flights": [], "price_insights": {"lowest_price": 6900}})
    assert f.live_price(_deal().quote) == 6900


def test_no_results_is_none_not_an_error():
    f, _ = _flights({"error": "Google Flights hasn't returned any results for this query."})
    assert f.live_price(_deal().quote) is None


def test_api_error_raises_without_the_key():
    f, _ = _flights({"error": "Invalid API key."}, status=401)
    with pytest.raises(ConfirmError) as exc:
        f.live_price(_deal().quote)
    assert "Invalid API key" in str(exc.value) and KEY not in str(exc.value)


def test_transport_error_never_leaks_the_key_in_the_url():
    f, _ = _flights(httpx.ConnectError(f"failed: https://serpapi.com/search?api_key={KEY}"))
    with pytest.raises(ConfirmError) as exc:
        f.live_price(_deal().quote)
    assert KEY not in str(exc.value)


def test_decisions():
    def result(payload):
        return check(_flights(payload)[0], _deal(7000))

    assert result({"best_flights": [{"price": 7600}]}).status == "confirmed"  # +8.6%
    assert result({"best_flights": [{"price": 7900}]}).status == "stale"  # +12.9%
    assert result({"best_flights": [{"price": 6000}]}).status == "confirmed"  # cheaper live
    assert result({"error": "hasn't returned any results"}).status == "unconfirmed"
    assert result({"error": "server exploded"}).status == "unconfirmed"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "t.db") as s:
        yield s


def test_confirm_alerts_splits_and_annotates(store):
    class Scripted:
        def __init__(self):
            self.prices = iter([7100, 9000, None])

        def live_price(self, q):
            return next(self.prices)

    confirmed, stale, unlisted = _deal(7000), _deal(7000), _deal(7000)
    send, dropped = confirm_alerts(
        [confirmed, stale, unlisted], Scripted(), store, monthly_cap=200, now=NOW
    )
    assert send == [confirmed, unlisted] and dropped == [stale]
    assert confirmed.live_price_inr == 7100
    assert "Rs 7,100 per person, return, checked live on Google Flights just now" in "\n".join(
        format_alert_lines(confirmed)
    )
    assert "not re-checked live (not listed on Google Flights)" in "\n".join(
        format_alert_lines(unlisted)
    )
    assert store.get_meta("live_checks:2026-11") == "3"


def test_monthly_cap_sends_unconfirmed_without_searching(store):
    class Never:
        def live_price(self, q):
            raise AssertionError("must not search past the cap")

    store.set_meta("live_checks:2026-11", "200")
    d = _deal()
    send, dropped = confirm_alerts([d], Never(), store, monthly_cap=200, now=NOW)
    assert send == [d] and not dropped
    assert "monthly limit of 200" in d.live_note


def test_cap_resets_each_calendar_month(store):
    store.set_meta("live_checks:2026-10", "200")

    class Ok:
        def live_price(self, q):
            return 7000

    d = _deal()
    confirm_alerts([d], Ok(), store, monthly_cap=200, now=NOW)
    assert d.live_price_inr == 7000


def test_no_key_sends_everything_labelled(store):
    d = _deal()
    send, dropped = confirm_alerts([d], None, store, monthly_cap=200, now=NOW)
    assert send == [d] and "no SerpApi key" in d.live_note


def test_poll_command_end_to_end_drops_stale_and_sends_confirmed(monkeypatch, tmp_path):
    """Through the real CLI: mock fares, faked SerpApi and Telegram."""
    from typer.testing import CliRunner

    import fareloot.confirm as confirm_mod
    import fareloot.notify.telegram as tg
    from fareloot.cli import app

    sent: list[str] = []
    monkeypatch.setattr(tg, "_send", lambda token, chat, text, **kw: sent.append(text))

    class HalfStale:
        """Confirms every other alert, reports the rest as having gone up 50%."""

        def __init__(self, key, client=None):
            self.n = 0

        def live_price(self, q):
            self.n += 1
            return q.price_inr + 100 if self.n % 2 else int(q.price_inr * 1.5)

    monkeypatch.setattr(confirm_mod, "SerpApiFlights", HalfStale)
    monkeypatch.chdir(tmp_path)
    for k, v in {
        "FARELOOT_TELEGRAM_BOT_TOKEN": "t",
        "FARELOOT_TELEGRAM_CHAT_ID": "1",
        "FARELOOT_SERPAPI_KEY": "k",
    }.items():
        monkeypatch.setenv(k, v)

    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["seed", "--provider", "mock"]).exit_code == 0
    r = runner.invoke(app, ["poll", "--provider", "mock", "--notify", "telegram", "--no-discovery"])
    assert r.exit_code == 0, r.output

    summary = next(line for line in r.output.splitlines() if line.startswith("Live check:"))
    confirmed = int(summary.split()[2])
    stale = int(summary.split("confirmed, ")[1].split()[0])
    assert confirmed > 0 and stale > 0
    text = "\n".join(sent)
    assert text.count("checked live on Google Flights") == confirmed

    # Delivered deals and stale ones are both recorded: the stale ones so the
    # same dead cache entry doesn't spend a search again next run.
    import sqlite3

    rows = sqlite3.connect("data/fareloot.db").execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    sent_count = int(summary.split()[-3])
    assert rows == confirmed + stale + sent_count


def test_live_quote_carries_googles_verdict():
    f, _ = _flights(
        {
            "best_flights": [{"price": 7250}],
            "price_insights": {"price_level": "low", "typical_price_range": [9000, 12500]},
        }
    )
    live = f.live_quote(_deal().quote)
    assert (live.price, live.level, live.typical) == (7250, "low", (9000, 12500))


def test_malformed_insights_are_ignored_not_fatal():
    f, _ = _flights(
        {
            "best_flights": [{"price": 7250}],
            "price_insights": {"price_level": 3, "typical_price_range": [690]},
        }
    )
    live = f.live_quote(_deal().quote)
    assert (live.price, live.level, live.typical) == (7250, "", None)


def test_confirmed_alert_shows_googles_verdict(store):
    f, _ = _flights(
        {
            "best_flights": [{"price": 7100}],
            "price_insights": {"price_level": "low", "typical_price_range": [9000, 12500]},
        }
    )
    d = _deal(7000)
    send, _ = confirm_alerts([d], f, store, monthly_cap=200, now=NOW)
    text = "\n".join(format_alert_lines(send[0]))
    assert "checked live on Google Flights" in text
    assert "Google also rates this fare low (usually Rs 9,000 to 12,500)" in text


def test_every_poll_says_how_close_it_came(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from fareloot.cli import app

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["init"])
    runner.invoke(app, ["seed", "--provider", "mock"])
    r = runner.invoke(app, ["poll", "--provider", "mock", "--notify", "console", "--no-discovery"])
    line = next(x for x in r.output.splitlines() if x.startswith("Scored "))
    assert "JACKPOT" in line and "Closest to an alert: hotness" in line and "STEAL needs 65" in line
    for code in ("COK", "GYD", "DXB", "BKK"):
        assert code not in line  # public logs must not name routes
