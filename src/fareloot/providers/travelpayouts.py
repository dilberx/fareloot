from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..models import Quote
from .base import parse_date, parse_dt

BASE_URL = "https://api.travelpayouts.com"
CURRENCY = "inr"

# The documented ceiling on /v2/prices/latest is 200 requests per hour per IP.
# Sitting well under it leaves room for retries without risking the token.
MAX_CALLS_PER_HOUR = 150
MIN_INTERVAL_S = 0.25
CACHE_TTL_S = 1800
V3_MAX_FAILURES = 3


class ProviderError(RuntimeError):
    pass


class RateLimiter:
    def __init__(self, per_hour: int = MAX_CALLS_PER_HOUR, min_interval: float = MIN_INTERVAL_S):
        self.per_hour = per_hour
        self.min_interval = min_interval
        self._calls: deque[float] = deque()
        self._last = 0.0

    def acquire(self) -> None:
        now = time.monotonic()
        gap = now - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)

        while self._calls and self._calls[0] < time.monotonic() - 3600:
            self._calls.popleft()
        if len(self._calls) >= self.per_hour:
            sleep_for = 3600 - (time.monotonic() - self._calls[0]) + 1
            if sleep_for > 0:
                time.sleep(sleep_for)
            while self._calls and self._calls[0] < time.monotonic() - 3600:
                self._calls.popleft()

        self._last = time.monotonic()
        self._calls.append(self._last)


class TravelpayoutsProvider:
    """Client for the Travelpayouts (Aviasales) Data API.

    The token goes in a header rather than the query string so it can never end
    up in a logged URL. Responses are served from a short-lived disk cache
    because the upstream data is itself a cache: re-fetching within the window
    costs quota and returns the same numbers.
    """

    name = "travelpayouts"

    def __init__(
        self,
        token: str,
        *,
        marker: str = "",
        cache_dir: Path | None = None,
        max_calls: int | None = None,
        client: httpx.Client | None = None,
        market: str = "",
    ):
        if not token:
            raise ProviderError("no Travelpayouts token - set FARELOOT_TP_TOKEN in .env")
        self._token = token
        self.marker = marker
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self.limiter = RateLimiter()
        self.max_calls = max_calls
        self.calls_made = 0
        # Aviasales caches prices per market. Empty lets the API infer it from
        # the origin; provider-check measures whether forcing one adds data.
        self.market = market
        # v3 is the replacement Travelpayouts recommends for /v1/prices/cheap.
        # Until it is proven on live data, a failure falls back to v1, and
        # after V3_MAX_FAILURES in one run v3 is skipped so a dead endpoint
        # doesn't double every call.
        self.v3_failures = 0
        self.fallbacks = 0
        self._client = client or httpx.Client(
            timeout=20.0, headers={"Accept-Encoding": "gzip, deflate"}
        )

    # -- plumbing --------------------------------------------------------

    def _cache_path(self, path: str, params: dict[str, Any]) -> Path | None:
        if not self.cache_dir:
            return None
        key = json.dumps([path, sorted(params.items())], sort_keys=True)
        return self.cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        cache_file = self._cache_path(path, params)
        if cache_file and cache_file.exists():
            if time.time() - cache_file.stat().st_mtime < CACHE_TTL_S:
                return json.loads(cache_file.read_text(encoding="utf-8"))

        max_attempts = 3
        resp: httpx.Response | None = None
        for attempt in range(max_attempts):
            if self.max_calls is not None and self.calls_made >= self.max_calls:
                raise ProviderError(f"call budget of {self.max_calls} exhausted")

            self.limiter.acquire()
            self.calls_made += 1
            try:
                resp = self._client.get(
                    f"{BASE_URL}{path}", params=params, headers={"X-Access-Token": self._token}
                )
            except httpx.TransportError as exc:
                if attempt == max_attempts - 1:
                    raise ProviderError(f"{path} request failed: {type(exc).__name__}") from None
                time.sleep(0.5 * (2**attempt))
                continue
            except httpx.HTTPError as exc:
                raise ProviderError(f"{path} request failed: {type(exc).__name__}") from None

            if resp.status_code == 429:
                if attempt == max_attempts - 1:
                    raise ProviderError(f"{path} returned HTTP 429 (rate limited)")
                retry_after = resp.headers.get("Retry-After")
                try:
                    sleep_s = float(retry_after) if retry_after else 1.0 * (2**attempt)
                except ValueError:
                    sleep_s = 1.0 * (2**attempt)
                time.sleep(min(sleep_s, 30.0))
                continue

            if resp.status_code in (500, 502, 503, 504):
                if attempt == max_attempts - 1:
                    raise ProviderError(f"{path} returned HTTP {resp.status_code}")
                time.sleep(0.5 * (2**attempt))
                continue

            if resp.status_code != 200:
                raise ProviderError(f"{path} returned HTTP {resp.status_code}")

            break

        if resp is None:
            raise ProviderError(f"{path} produced no response")
        try:
            payload = resp.json()
        except ValueError:
            raise ProviderError(f"{path} returned a non-JSON body") from None
        if isinstance(payload, dict) and payload.get("success") is False:
            raise ProviderError(f"{path} reported failure: {payload.get('error', 'unknown')}")

        if cache_file:
            cache_file.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    # -- endpoints -------------------------------------------------------

    def cheap(
        self, origin: str, destination: str, depart: str, return_: str | None = None
    ) -> list[Quote]:
        if self.v3_failures < V3_MAX_FAILURES:
            try:
                return self.prices_for_dates(origin, destination, depart, return_)
            except ProviderError as exc:
                if "call budget" in str(exc):
                    raise
                self.v3_failures += 1
                self.fallbacks += 1
        return self.cheap_v1(origin, destination, depart, return_)

    def prices_for_dates(
        self,
        origin: str,
        destination: str,
        depart: str,
        return_: str | None = None,
        *,
        market: str | None = None,
    ) -> list[Quote]:
        """Cheapest cached fares for a route and month, via the v3 endpoint."""
        market = self.market if market is None else market
        payload = self._get(
            "/aviasales/v3/prices_for_dates",
            {
                "origin": origin,
                "destination": destination,
                "departure_at": depart,
                "return_at": return_,
                "one_way": "false" if return_ else "true",
                "direct": "false",
                "currency": CURRENCY,
                "market": market or None,
                "sorting": "price",
                "unique": "false",
                "limit": 100,
                "page": 1,
            },
        )
        return self._parse_v3(payload, origin, destination)

    def cheap_v1(
        self, origin: str, destination: str, depart: str, return_: str | None = None
    ) -> list[Quote]:
        """Deprecated by Travelpayouts; kept as the fallback for prices_for_dates."""
        payload = self._get(
            "/v1/prices/cheap",
            {
                "origin": origin,
                "destination": destination,
                "depart_date": depart,
                "return_date": return_,
                "currency": CURRENCY,
            },
        )
        return self._parse_keyed(payload, origin)

    def month_matrix(self, origin: str, destination: str, month: str) -> list[Quote]:
        payload = self._get(
            "/v2/prices/month-matrix",
            {
                "origin": origin,
                "destination": destination,
                "month": f"{month}-01",
                "currency": CURRENCY,
                "show_to_affiliates": "true",
            },
        )
        return self._parse_array(payload)

    def city_directions(self, origin: str) -> list[Quote]:
        payload = self._get(
            "/v1/city-directions", {"origin": origin, "currency": CURRENCY}
        )
        return self._parse_keyed(payload, origin)

    def latest(
        self, origin: str, *, one_way: bool = False, trip_duration: int | None = None
    ) -> list[Quote]:
        payload = self._get(
            "/v2/prices/latest",
            {
                "origin": origin,
                "currency": CURRENCY,
                "one_way": "true" if one_way else "false",
                "trip_duration": trip_duration,
                "limit": 1000,
                "sorting": "price",
                "show_to_affiliates": "true",
            },
        )
        return self._parse_array(payload)

    # -- parsing ---------------------------------------------------------

    @staticmethod
    def _parse_v3(payload: dict[str, Any], origin: str, destination: str) -> list[Quote]:
        """Parse prices_for_dates. Keys come from the request, not the response.

        The response names cities and airports separately; the request's codes
        are the ones the rest of fareloot keys routes by.
        """
        data = payload.get("data")
        if not isinstance(data, list):
            raise ProviderError("prices_for_dates returned an unexpected shape")
        now = datetime.now(UTC)
        out: list[Quote] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            depart = parse_date(item.get("departure_at"))
            try:
                price = int(float(item.get("price")))
            except (TypeError, ValueError):
                continue
            if depart is None or price <= 0:
                continue
            out.append(
                Quote(
                    origin=origin.upper(),
                    destination=destination.upper(),
                    depart_date=depart,
                    return_date=parse_date(item.get("return_at")),
                    price_inr=price,
                    stops=item.get("transfers"),
                    airline=item.get("airline"),
                    observed_at=now,
                )
            )
        return out

    @staticmethod
    def _parse_array(payload: dict[str, Any]) -> list[Quote]:
        """For endpoints returning data as a flat list (month-matrix, latest)."""
        now = datetime.now(UTC)
        out: list[Quote] = []
        data = payload.get("data")
        if not isinstance(data, list):
            return out
        for item in data:
            if not isinstance(item, dict):
                continue
            depart = parse_date(item.get("depart_date"))
            price = item.get("value", item.get("price"))
            if depart is None or price is None or price == "":
                continue
            try:
                price_inr = int(float(price))
            except (ValueError, TypeError):
                continue
            if price_inr <= 0:
                continue
            out.append(
                Quote(
                    origin=str(item.get("origin", "")).upper(),
                    destination=str(item.get("destination", "")).upper(),
                    depart_date=depart,
                    return_date=parse_date(item.get("return_date")),
                    price_inr=price_inr,
                    stops=item.get("number_of_changes"),
                    airline=item.get("gate") or item.get("airline"),
                    distance_km=item.get("distance"),
                    expires_at=parse_dt(item.get("expires_at")),
                    observed_at=now,
                )
            )
        return out

    @staticmethod
    def _parse_keyed(payload: dict[str, Any], origin: str) -> list[Quote]:
        """For endpoints keyed by destination (cheap, city-directions).

        `cheap` nests one level deeper, keyed by number of stops; the two shapes
        are handled together because the leaf objects are identical.
        """
        now = datetime.now(UTC)
        out: list[Quote] = []
        data = payload.get("data")
        if not isinstance(data, dict):
            return out
        for destination, value in data.items():
            if not isinstance(value, dict):
                continue
            leaves = (
                list(value.values())
                if all(isinstance(v, dict) for v in value.values()) and value
                else [value]
            )
            for leaf in leaves:
                if not isinstance(leaf, dict) or leaf.get("price") is None:
                    continue
                depart = parse_date(leaf.get("departure_at"))
                if depart is None:
                    continue
                try:
                    price_inr = int(float(leaf["price"]))
                except (ValueError, TypeError):
                    continue
                if price_inr <= 0:
                    continue
                out.append(
                    Quote(
                        origin=str(leaf.get("origin", origin)).upper(),
                        destination=str(destination).upper(),
                        depart_date=depart,
                        return_date=parse_date(leaf.get("return_at")),
                        price_inr=price_inr,
                        stops=leaf.get("transfers", leaf.get("number_of_changes")),
                        airline=leaf.get("airline"),
                        distance_km=leaf.get("distance"),
                        expires_at=parse_dt(leaf.get("expires_at")),
                        observed_at=now,
                    )
                )
        return out
