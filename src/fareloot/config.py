from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import Origin, Reach
from .scoring import Thresholds

DEFAULT_CONFIG_PATH = Path("config/watchlist.yaml")
DEFAULT_DB_PATH = Path("data/fareloot.db")
DEFAULT_CACHE_DIR = Path(".cache")


class Settings(BaseSettings):
    """Credentials, from the environment or a local .env file."""

    model_config = SettingsConfigDict(
        env_prefix="FARELOOT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    tp_token: str = ""
    tp_marker: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @property
    def has_provider(self) -> bool:
        return bool(self.tp_token)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


class ReachModel(BaseModel):
    mode: str = "train"
    hours: float
    cost_inr: int

    def to_reach(self) -> Reach:
        return Reach(mode=self.mode, hours=self.hours, cost_inr=self.cost_inr)


class OriginModel(BaseModel):
    iata: str
    name: str
    reach: ReachModel

    def to_origin(self) -> Origin:
        return Origin(iata=self.iata.upper(), name=self.name, reach=self.reach.to_reach())


class TierThresholds(BaseModel):
    jackpot: int = 95
    loot: int = 80
    steal: int = 65
    deal: int = 50


class AlertSettings(BaseModel):
    min_saving_inr: int = 3_000
    min_pct_below_baseline: float = 25.0
    cooldown_hours: int = 48
    redrop_pct: float = 10.0
    tiers: TierThresholds = Field(default_factory=TierThresholds)


class LowestFareRule(BaseModel):
    """Alert on destinations you fly often when a round trip is simply the
    cheapest seen recently, without waiting for a statistical crash.

    The return leg must still be checked and healthy, as for every alert.
    """

    destinations: list[str]
    window_days: int = 30
    min_saving_inr: int = 1_500
    min_pct_below_baseline: float = 10.0
    # A "lowest in 30 days" claim is meaningless on a route with two days of
    # history, so the rule stays dormant until the window holds enough prices.
    min_history: int = 20
    min_history_days: int = 7


class Watchlist(BaseModel):
    origins: list[OriginModel]
    destinations: list[str] = Field(default_factory=list)
    max_reach_hours: float = 14.0
    train_hour_value_inr: float = 80.0
    trip_bands: list[tuple[int, int]] = Field(
        default_factory=lambda: [(3, 6), (7, 10), (11, 21)]
    )
    search_horizon_months: int = 6
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    lowest_fare_rules: list[LowestFareRule] = Field(default_factory=list)

    @field_validator("lowest_fare_rules")
    @classmethod
    def _one_rule_per_destination(cls, rules: list[LowestFareRule]) -> list[LowestFareRule]:
        seen: set[str] = set()
        for rule in rules:
            rule.destinations = [d.upper() for d in rule.destinations]
            dupes = seen.intersection(rule.destinations)
            if dupes:
                raise ValueError(f"destination in more than one lowest_fare_rule: {sorted(dupes)}")
            seen.update(rule.destinations)
        return rules

    def lowest_rule_for(self, destination: str) -> LowestFareRule | None:
        for rule in self.lowest_fare_rules:
            if destination.upper() in rule.destinations:
                return rule
        return None

    @field_validator("trip_bands")
    @classmethod
    def _bands_are_ordered_and_disjoint(cls, bands: list[tuple[int, int]]) -> list[tuple[int, int]]:
        bands = sorted(bands)
        for lo, hi in bands:
            if lo < 1 or hi < lo:
                raise ValueError(f"trip band {lo}-{hi} must satisfy 1 <= low <= high")
        for (_, prev_hi), (lo, _) in zip(bands, bands[1:], strict=False):
            if lo <= prev_hi:
                raise ValueError("trip bands must not overlap")
        return bands

    def band_for(self, trip_length: int) -> tuple[int, int] | None:
        """The band a round trip pools its baseline with, or None to discard it."""
        for lo, hi in self.trip_bands:
            if lo <= trip_length <= hi:
                return (lo, hi)
        return None

    def origin_map(self) -> dict[str, Origin]:
        return {o.iata.upper(): o.to_origin() for o in self.origins}

    def thresholds(self) -> Thresholds:
        a = self.alerts
        return Thresholds(
            jackpot=a.tiers.jackpot,
            loot=a.tiers.loot,
            steal=a.tiers.steal,
            deal=a.tiers.deal,
            min_saving_inr=a.min_saving_inr,
            min_pct_below_baseline=a.min_pct_below_baseline,
        )


class WatchlistError(ValueError):
    """A watchlist that cannot be loaded, described without echoing its contents."""


def load_watchlist(path: Path = DEFAULT_CONFIG_PATH) -> Watchlist:
    """Load the watchlist, raising WatchlistError with locations but no values.

    In deployment the file comes from a secret and errors land in a public
    Actions log. pydantic re-renders invalid entries as a dict, which GitHub's
    secret masking cannot match, so neither parser's message is passed through.
    """
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - run `fareloot init` first")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark else ""
        raise WatchlistError(f"watchlist is not valid YAML{where}") from None
    try:
        return Watchlist.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise WatchlistError(f"watchlist is invalid - {problems}") from None


DEFAULT_WATCHLIST_YAML = """\
# Airports you can realistically depart from, and what it costs to get there.
# Times and fares are one-way estimates; fareloot doubles them for a round trip.
origins:
  - { iata: COK, name: Kochi,       reach: { mode: local,           hours: 1,   cost_inr: 200 } }
  - { iata: TRV, name: Trivandrum,  reach: { mode: train,           hours: 4.5, cost_inr: 400 } }
  - { iata: CCJ, name: Kozhikode,   reach: { mode: train,           hours: 4,   cost_inr: 350 } }
  - { iata: CNN, name: Kannur,      reach: { mode: train,           hours: 6,   cost_inr: 450 } }
  - { iata: CJB, name: Coimbatore,  reach: { mode: train,           hours: 5,   cost_inr: 450 } }
  - { iata: IXM, name: Madurai,     reach: { mode: overnight_train, hours: 8,   cost_inr: 600 } }
  - { iata: IXE, name: Mangalore,   reach: { mode: overnight_train, hours: 10,  cost_inr: 800 } }
  - { iata: BLR, name: Bangalore,   reach: { mode: overnight_train, hours: 12,  cost_inr: 900 } }
  - { iata: MAA, name: Chennai,     reach: { mode: overnight_train, hours: 14,  cost_inr: 1000 } }

# Places worth watching closely. Discovery scanning finds the rest on its own.
destinations:
  - BKK  # Bangkok
  - HKT  # Phuket
  - KUL  # Kuala Lumpur
  - SIN  # Singapore
  - CGK  # Jakarta
  - DPS  # Bali
  - SGN  # Ho Chi Minh City
  - HAN  # Hanoi
  - CMB  # Colombo
  - MLE  # Male
  - KTM  # Kathmandu
  - DXB  # Dubai
  - AUH  # Abu Dhabi
  - DOH  # Doha
  - MCT  # Muscat
  - GYD  # Baku
  - TBS  # Tbilisi
  - EVN  # Yerevan
  - ALA  # Almaty
  - TAS  # Tashkent
  - IST  # Istanbul
  - CAI  # Cairo
  - NBO  # Nairobi
  - HKG  # Hong Kong
  - DEL  # Delhi
  - BOM  # Mumbai
  - GOI  # Goa

# How far you are willing to travel overland to reach a cheaper airport.
max_reach_hours: 14

# What an hour on a train is worth to you, used only to rank options against
# each other. It is never shown as money and never added to a quoted price.
train_hour_value_inr: 80

# Round trips are only kept if their length in days falls inside one of these
# bands, and each one is judged against the history of its whole band. Real fare
# data comes back in dozens of different trip lengths, including same-day and
# overnight returns that aren't trips anyone takes, so pooling keeps baselines
# deep and dropping the rest keeps cheap non-trips from alerting.
trip_bands: [[3, 6], [7, 10], [11, 21]]
search_horizon_months: 6

# Destinations you fly often can also alert when a round trip is simply the
# cheapest seen recently, not only on a big drop. The return leg must still be
# healthy, and a route needs a week of history before "lowest" counts.
# lowest_fare_rules:
#   - destinations: [AAA, BBB]
#     window_days: 30
#     min_saving_inr: 1500
#     min_pct_below_baseline: 10

alerts:
  min_saving_inr: 3000
  min_pct_below_baseline: 25
  cooldown_hours: 48
  redrop_pct: 10
  tiers:
    jackpot: 95
    loot: 80
    steal: 65
    deal: 50
"""
