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
