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
