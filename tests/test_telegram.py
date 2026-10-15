"""The bot token sits in Telegram's URL path and this repo's Actions logs are
public, so no error path may carry the request URL out of _send().
"""

import httpx
import pytest

from fareloot.notify import telegram

TOKEN = "123456789:AAtotally-fake-bot-token-value"


def test_transport_error_does_not_leak_the_token(monkeypatch):
    def boom(*a, **kw):
        # Mirrors httpx: the exception text carries the full request URL.
        raise httpx.ConnectError(f"failed to connect to {telegram.API.format(token=TOKEN)}")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(telegram.TelegramError) as exc:
        telegram._send(TOKEN, "42", "hi")
    assert TOKEN not in str(exc.value)
    assert "ConnectError" in str(exc.value)


def test_http_error_does_not_leak_the_token(monkeypatch):
    class Resp:
        status_code = 401
        text = '{"ok":false,"error_code":401,"description":"Unauthorized"}'

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: Resp())
    with pytest.raises(telegram.TelegramError) as exc:
        telegram._send(TOKEN, "42", "hi")
    assert TOKEN not in str(exc.value)
    assert "401" in str(exc.value)


def test_missing_credentials_are_rejected_before_any_request(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("should not have issued a request")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(telegram.TelegramError):
        telegram._send("", "42", "hi")
    with pytest.raises(telegram.TelegramError):
        telegram._send(TOKEN, "", "hi")


def _deal(tier_name, price):
    from datetime import date

    from fareloot.models import Deal, Origin, Quote, Reach, Tier

    q = Quote(origin="COK", destination="GYD", depart_date=date(2026, 12, 1),
              return_date=date(2026, 12, 8), price_inr=price)
    o = Origin(iata="COK", name="Kochi", reach=Reach(mode="local", hours=1, cost_inr=200))
    return Deal(quote=q, origin=o, hotness=90, tier=Tier[tier_name], baseline_inr=20000,
                saving_inr=20000 - price, pct_below=50.0, one_way_only=False, reasons=["x"])


def test_partial_send_reports_exactly_what_was_delivered(monkeypatch):
    sent = []

    def flaky(token, chat, text):
        if len(sent) == 1:
            raise telegram.TelegramError("Telegram API returned HTTP 502: down")
        sent.append(text)

    monkeypatch.setattr(telegram, "_send", flaky)
    first, second, third = _deal("LOOT", 7000), _deal("LOOT", 7100), _deal("STEAL", 9000)
    with pytest.raises(telegram.TelegramError) as exc:
        telegram.send_deals(TOKEN, "42", [first, second, third])
    assert exc.value.delivered == [first]


def test_many_steals_are_packed_whole_never_split_mid_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "_send", lambda token, chat, text: sent.append(text))
    deals = [_deal("STEAL", 9000 + i) for i in range(60)]
    delivered = telegram.send_deals(TOKEN, "42", deals)
    assert delivered == deals
    assert len(sent) > 1
    assert all(len(t) <= telegram.MAX_LEN for t in sent)
    # Every alert's booking link survives intact in exactly one message.
    assert sum(t.count("aviasales.com/search/") for t in sent) == 60
