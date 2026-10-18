from __future__ import annotations

import httpx

from ..models import Deal
from .message import render, render_short

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3800  # Telegram's limit is 4096; leave headroom for markdown escapes


class TelegramError(RuntimeError):
    def __init__(self, message: str, delivered: list[Deal] | None = None):
        super().__init__(message)
        # Deals that reached Telegram before the failure, so the caller can still
        # record those and leave the rest eligible to alert again next run.
        self.delivered = delivered or []


def _send(bot_token: str, chat_id: str, text: str, *, html: bool = False) -> None:
    if not bot_token or not chat_id:
        raise TelegramError("Telegram bot token or chat id is not configured")
    try:
        resp = httpx.post(
            API.format(token=bot_token),
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
                **({"parse_mode": "HTML"} if html else {}),
            },
            timeout=15.0,
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # Telegram's API puts the bot token in the URL path, and httpx includes
        # the request URL in its exception text. This repo's Actions logs are
        # public, so the original exception must not propagate. InvalidURL is
        # not an HTTPError subclass, so it is named separately.
        raise TelegramError(f"Telegram request failed: {type(exc).__name__}") from None
    if resp.status_code != 200:
        # resp.text is Telegram's JSON error body, which carries no credentials.
        raise TelegramError(f"Telegram API returned HTTP {resp.status_code}: {resp.text[:200]}")


def _pack(deals: list[Deal], marker: str) -> list[tuple[str, list[Deal]]]:
    """Group whole deals into messages under Telegram's length limit.

    Splitting by character count could cut an alert or its link in half, and
    made it impossible to say which deals a failed send had already delivered.
    """
    messages: list[tuple[str, list[Deal]]] = []
    text, group = f"<b>{len(deals)} good drop(s) this run</b>", []
    for deal in deals:
        block = render_short(deal, marker, html=True)
        if group and len(text) + 2 + len(block) > MAX_LEN:
            messages.append((text, group))
            text, group = "(continued)", []
        text += "\n\n" + block
        group.append(deal)
    if group:
        messages.append((text, group))
    return messages


def send_deals(bot_token: str, chat_id: str, deals: list[Deal], marker: str = "") -> list[Deal]:
    """Send alerts and return the deals that were delivered.

    On failure, raises TelegramError with the deals delivered so far attached.
    """
    jackpots = [d for d in deals if d.tier.value in ("JACKPOT", "LOOT")]
    steals = [d for d in deals if d.tier.value == "STEAL"]
    batches = [(render(d, marker, html=True), [d]) for d in jackpots]
    batches += _pack(steals, marker)

    delivered: list[Deal] = []
    for text, group in batches:
        try:
            _send(bot_token, chat_id, text, html=True)
        except TelegramError as exc:
            raise TelegramError(str(exc), delivered) from None
        delivered += group
    return delivered


def sample_alert() -> str:
    """A realistic alert in the live HTML format, so a test proves Telegram accepts it.

    Telegram rejects malformed HTML outright ("can't parse entities"), which would
    otherwise only show up as a lost real alert.
    """
    from datetime import date, timedelta

    from ..models import Origin, Quote, Reach, Tier

    out = date.today() + timedelta(days=30)
    q = Quote(
        origin="BLR",
        destination="DXB",
        depart_date=out,
        return_date=out + timedelta(days=7),
        price_inr=11900,
    )
    o = Origin(
        iata="BLR", name="Bangalore", reach=Reach(mode="overnight_train", hours=12, cost_inr=900)
    )
    deal = Deal(
        quote=q,
        origin=o,
        hotness=88,
        tier=Tier.LOOT,
        baseline_inr=24800,
        saving_inr=12800,
        pct_below=51.6,
        one_way_only=False,
        reasons=[],
        return_status="healthy",
        live_price_inr=12100,
        google_level="low",
        google_typical=(19000, 26500),
    )
    return "<b>TEST ALERT</b>, not a real fare. This is what a deal looks like:\n\n" + render(
        deal, html=True
    )


def send_test(bot_token: str, chat_id: str) -> None:
    _send(bot_token, chat_id, sample_alert(), html=True)
