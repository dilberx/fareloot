"""Alerts must make sense to someone who has never seen fareloot's internals."""

from datetime import date

from fareloot.models import Deal, Origin, Quote, Reach, Tier
from fareloot.notify.message import render, render_short

KOCHI = Origin(iata="COK", name="Kochi", reach=Reach(mode="local", hours=1, cost_inr=200))
BANGALORE = Origin(
    iata="BLR", name="Bangalore", reach=Reach(mode="overnight_train", hours=12, cost_inr=900)
)


def _deal(origin=KOCHI, dest="GYD", **kw):
    q = Quote(
        origin=origin.iata,
        destination=dest,
        depart_date=date(2026, 11, 19),
        return_date=date(2026, 11, 26),
        price_inr=6699,
    )
    fields = dict(
        quote=q,
        origin=origin,
        hotness=85,
        tier=Tier.LOOT,
        baseline_inr=18284,
        saving_inr=11585,
        pct_below=63.4,
        one_way_only=False,
        reasons=["usually Rs 18,284 (63% below)", "return leg normal (z +0.8)"],
        return_status="healthy",
    )
    fields.update(kw)
    return Deal(**fields)


def test_plain_language_no_scoring_internals():
    text = render(_deal(live_price_inr=6800))
    assert text.startswith("LOOT: Kochi to Baku, Rs 6,800 return")
    assert "63% below the usual Rs 18,284 for this route, a big drop" in text
    assert "Out Thu 19 Nov, back Thu 26 Nov (7 nights)" in text
    assert "Rs 6,800 per person, return, checked live on Google Flights just now" in text
    assert "The return flight is normally priced, not a trap" in text
    assert "disappear within hours" in text
    for jargon in ("z +", "hotness", " 85", "COK", "GYD", "->"):
        assert jargon not in text.split("Book on")[0], jargon


def test_live_price_is_the_headline_price():
    text = render(_deal(live_price_inr=6800))
    assert "Rs 6,800" in text.splitlines()[0] and "6,699" not in text


def test_train_leg_and_door_to_door_cost():
    text = render(_deal(origin=BANGALORE, live_price_inr=6800))
    assert "Getting to Bangalore: overnight train, about 12h, Rs 900 each way" in text
    assert "Door to door: about Rs 8,600" in text  # 6,800 + 2 x 900
    assert "Getting to" not in render(_deal(live_price_inr=6800))  # Kochi is local


def test_unconfirmed_price_says_so_and_why():
    text = render(_deal(live_note="not live-checked: monthly limit of 200 searches reached"))
    assert "Rs 6,699 per person, return. Seen in the last few hours and not re-checked live" in text
    assert "(monthly limit of 200 searches reached), so it may have changed" in text


def test_unchecked_return_is_stated_not_hidden():
    assert "return flight's price wasn't checked separately" in render(
        _deal(return_status="unchecked")
    )


def test_lowest_in_window_and_google_verdict():
    text = render(
        _deal(
            dest="DXB",
            tier=Tier.STEAL,
            lowest_in_window=True,
            lowest_window_days=30,
            live_price_inr=6800,
            google_level="low",
            google_typical=(9000, 12500),
        )
    )
    assert "The cheapest Kochi to Dubai return in 30 days" in text
    assert "Google also rates this fare low (usually Rs 9,000 to 12,500)" in text
    assert "disappear within hours" not in text  # urgency only for LOOT and JACKPOT


def test_html_form_escapes_text_and_labels_links():
    html = render(_deal(live_note="not live-checked: live check failed: <bad> & worse"), html=True)
    assert html.startswith("<b>LOOT: Kochi to Baku")
    assert "&lt;bad&gt; &amp; worse" in html and "<bad>" not in html
    assert (
        '<a href="https://www.google.com/travel/flights?q=' in html
        and ">Book on Google Flights</a>" in html
    )
    assert ">Compare on Aviasales</a>" in html


def test_short_form_for_batches():
    text = render_short(_deal(origin=BANGALORE, live_price_inr=6800))
    assert text.splitlines()[0] == "Bangalore to Baku, Rs 6,800 return, 63% below usual"
    assert "Overnight train to Bangalore" in text and "Price checked live on Google Flights" in text
    assert "Not re-checked live" in render_short(_deal())


def test_unknown_codes_fall_back_to_the_code():
    assert render(_deal(dest="XQZ")).startswith("LOOT: Kochi to XQZ")
