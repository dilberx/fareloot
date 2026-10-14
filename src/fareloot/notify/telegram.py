from __future__ import annotations

import httpx

from ..models import Deal
from ..watcher import format_alert_lines

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3800  # Telegram's limit is 4096; leave headroom for markdown escapes


class TelegramError(RuntimeError):
    pass


def _send(bot_token: str, chat_id: str, text: str) -> None:
    if not bot_token or not chat_id:
        raise TelegramError("Telegram bot token or chat id is not configured")
    try:
        resp = httpx.post(
            API.format(token=bot_token),
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        # Telegram's API puts the bot token in the URL path, and httpx includes
        # the request URL in its exception text. This repo's Actions logs are
        # public, so the original exception must not propagate.
        raise TelegramError(f"Telegram request failed: {type(exc).__name__}") from None
    if resp.status_code != 200:
        # resp.text is Telegram's JSON error body, which carries no credentials.
        raise TelegramError(f"Telegram API returned HTTP {resp.status_code}: {resp.text[:200]}")


def send_deals(bot_token: str, chat_id: str, deals: list[Deal], marker: str = "") -> None:
    if not deals:
        return
    jackpots = [d for d in deals if d.tier.value in ("JACKPOT", "LOOT")]
    steals = [d for d in deals if d.tier.value == "STEAL"]

    for deal in jackpots:
        _send(bot_token, chat_id, "\n".join(format_alert_lines(deal, marker)))

    if steals:
        chunks = []
        for deal in steals:
            chunks.append("\n".join(format_alert_lines(deal, marker)))
        message = f"{len(steals)} steal(s) this run:\n\n" + "\n\n".join(chunks)
        for start in range(0, len(message), MAX_LEN):
            _send(bot_token, chat_id, message[start : start + MAX_LEN])


def send_test(bot_token: str, chat_id: str) -> None:
    _send(bot_token, chat_id, "fareloot: this is a test alert. If you can read this, alerts work.")
