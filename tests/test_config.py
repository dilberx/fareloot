"""config/watchlist.yaml is gitignored, so the built-in template is now the only
copy under version control. Nothing else would catch it rotting.
"""

import pytest
import yaml

from fareloot.config import DEFAULT_WATCHLIST_YAML, Watchlist, WatchlistError, load_watchlist


def _default() -> Watchlist:
    return Watchlist.model_validate(yaml.safe_load(DEFAULT_WATCHLIST_YAML))


def test_default_template_is_valid_yaml_and_schema():
    wl = _default()
    assert wl.origins
    assert wl.destinations
    assert wl.trip_bands


def test_default_template_origins_are_usable():
    wl = _default()
    for o in wl.origins:
        assert len(o.iata) == 3, o.iata
        assert o.reach.hours >= 0
        assert o.reach.cost_inr >= 0
        # An origin beyond max_reach_hours would be scored but never reachable.
        assert o.reach.hours <= wl.max_reach_hours, o.iata


def test_default_template_has_no_self_referential_routes():
    wl = _default()
    origins = {o.iata.upper() for o in wl.origins}
    assert origins.isdisjoint({d.upper() for d in wl.destinations})


def test_default_template_thresholds_are_ordered():
    t = _default().alerts.tiers
    assert t.jackpot > t.loot > t.steal > t.deal


def test_load_watchlist_round_trips_the_template(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text(DEFAULT_WATCHLIST_YAML, encoding="utf-8")
    assert load_watchlist(path).origins == _default().origins


def test_load_watchlist_points_at_init_when_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="fareloot init"):
        load_watchlist(tmp_path / "nope.yaml")


def test_old_watchlist_with_trip_lengths_still_loads(tmp_path):
    # Deployed watchlists live in a repo secret written before trip_bands existed.
    old = DEFAULT_WATCHLIST_YAML.replace(
        "trip_bands: [[3, 6], [7, 10], [11, 21]]", "trip_lengths: [7, 10, 14]"
    )
    assert "trip_lengths" in old
    path = tmp_path / "watchlist.yaml"
    path.write_text(old, encoding="utf-8")
    assert load_watchlist(path).trip_bands == [(3, 6), (7, 10), (11, 21)]


def test_band_for_rejects_non_trips_and_pools_the_rest():
    wl = _default()
    assert wl.band_for(0) is None
    assert wl.band_for(2) is None
    assert wl.band_for(30) is None
    assert wl.band_for(9) == wl.band_for(7) == (7, 10)


def test_overlapping_bands_are_rejected():
    raw = yaml.safe_load(DEFAULT_WATCHLIST_YAML)
    raw["trip_bands"] = [[3, 8], [7, 10]]
    with pytest.raises(ValueError, match="overlap"):
        Watchlist.model_validate(raw)


def test_invalid_watchlist_error_names_the_field_but_not_its_values(tmp_path):
    # The deployed watchlist is a secret, and this error lands in a public log.
    bad = DEFAULT_WATCHLIST_YAML.replace("name: Trivandrum,", "")
    path = tmp_path / "watchlist.yaml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(WatchlistError) as exc:
        load_watchlist(path)
    msg = str(exc.value)
    assert "origins.1.name" in msg
    for leaked in ("TRV", "4.5", "400", "train"):
        assert leaked not in msg, leaked


def test_malformed_yaml_error_does_not_echo_lines(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text("origins:\n  - { iata: COK, name: Kochi\n", encoding="utf-8")
    with pytest.raises(WatchlistError) as exc:
        load_watchlist(path)
    assert "COK" not in str(exc.value) and "Kochi" not in str(exc.value)
