from fareloot.cli import seed_range_label


def test_seed_label_within_range():
    assert seed_range_label(126, 63, 486) == "tasks 126-189 of 486"


def test_seed_label_splits_when_the_cursor_wraps():
    # Previously printed "tasks 441-504 of 486".
    assert seed_range_label(441, 63, 486) == "tasks 441-486 and 0-18 of 486"


def test_seed_label_ending_exactly_at_total_does_not_split():
    assert seed_range_label(423, 63, 486) == "tasks 423-486 of 486"


def test_provider_check_reports_each_endpoint_and_survives_a_failure(monkeypatch):
    from typer.testing import CliRunner

    import fareloot.cli as cli
    from fareloot.providers.travelpayouts import ProviderError, TravelpayoutsProvider
    from tests.test_providers import V3_RESPONSE, _FakeClient

    class V1Dead(TravelpayoutsProvider):
        def cheap_v1(self, *a, **k):
            raise ProviderError("/v1/prices/cheap returned HTTP 410")

    monkeypatch.setattr(
        cli, "_make_provider", lambda *a, **k: V1Dead("t", client=_FakeClient(V3_RESPONSE))
    )
    r = CliRunner().invoke(cli.app, ["provider-check"])
    assert r.exit_code == 0, r.output
    assert (
        "v1 cheap, round trip" in r.output
        and "FAILED: /v1/prices/cheap returned HTTP 410" in r.output
    )
    assert r.output.count("1 fares, cheapest Rs 17,850") == 3


def _scored(hotness, baseline, price):
    from tests.test_telegram import _deal

    d = _deal("STEAL", price)
    d.hotness, d.baseline_inr = hotness, baseline
    d.saving_inr = max(0, baseline - price) if baseline else 0
    d.pct_below = d.saving_inr / baseline * 100 if baseline else 0.0
    return d


def test_closeness_ignores_high_hotness_fares_under_the_price_floors():
    # The bug: a fare 2% below usual, scored 84 on value alone, was reported as
    # "closest to an alert" although the floors meant it could never alert.
    from fareloot.cli import _closeness
    from fareloot.config import AlertSettings

    line = _closeness([_scored(84, 20000, 19600), _scored(40, 20000, 14000)], AlertSettings())
    assert "Closest to an alert: hotness 40 (30% below usual)" in line
    assert "1 cleared the price floors" in line and "84" not in line


def test_closeness_reports_the_biggest_drop_when_nothing_clears_the_floors():
    from fareloot.cli import _closeness
    from fareloot.config import AlertSettings

    line = _closeness([_scored(84, 20000, 19600), _scored(30, 20000, 17000)], AlertSettings())
    assert "No fare was 25% and Rs 3,000 below its usual price" in line
    assert "biggest drop was 15% (Rs 3,000)" in line and "Closest" not in line


def test_closeness_without_any_baseline_says_so():
    from fareloot.cli import _closeness
    from fareloot.config import AlertSettings

    line = _closeness([_scored(60, None, 9000)], AlertSettings())
    assert "None has enough history to judge yet" in line
