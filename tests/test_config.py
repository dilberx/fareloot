"""config/watchlist.yaml is gitignored, so the built-in template is now the only
copy under version control. Nothing else would catch it rotting.
"""

import pytest
import yaml

from fareloot.config import DEFAULT_WATCHLIST_YAML, Watchlist, load_watchlist


def _default() -> Watchlist:
    return Watchlist.model_validate(yaml.safe_load(DEFAULT_WATCHLIST_YAML))


def test_default_template_is_valid_yaml_and_schema():
    wl = _default()
    assert wl.origins
    assert wl.destinations
    assert wl.trip_lengths


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
