"""Parsing tests against realistic response shapes.

These are hand-built from the documented Travelpayouts response shapes rather
than recorded live traffic, since the API requires a signed-up token to call.
"""

import time

import pytest

from fareloot.providers.travelpayouts import ProviderError, TravelpayoutsProvider

CHEAP_RESPONSE = {
    "success": True,
    "data": {
        "GYD": {
            "0": {
                "origin": "COK",
                "departure_at": "2026-11-12T03:15:00Z",
                "return_at": "2026-11-26T14:40:00Z",
                "price": 8992,
                "transfers": 0,
                "airline": "6E",
                "expires_at": "2026-09-20T00:00:00Z",
            }
        }
    },
}

MONTH_MATRIX_RESPONSE = {
    "success": True,
    "data": [
        {
            "depart_date": "2026-11-05",
            "value": 19200,
            "distance": 3050,
            "number_of_changes": 0,
        },
        {
            "depart_date": "2026-11-06",
            "value": 0,  # a zero-price entry must be dropped, not treated as free
        },
    ],
}

LATEST_RESPONSE = {
    "success": True,
    "data": [
        {
            "origin": "COK",
            "destination": "TBS",
            "depart_date": "2026-11-10",
            "return_date": "2026-11-20",
            "value": 21000,
            "distance": 3400,
        }
    ],
}


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        return _FakeResponse(self.payload)


def _provider(payload):
    client = _FakeClient(payload)
    p = TravelpayoutsProvider("fake-token", client=client)
    return p, client


def test_cheap_parses_nested_stop_buckets():
    p, client = _provider(CHEAP_RESPONSE)
    quotes = p.cheap("COK", "GYD", "2026-11", "2026-11")
    assert len(quotes) == 1
    q = quotes[0]
    assert q.origin == "COK"
    assert q.destination == "GYD"
    assert q.price_inr == 8992
    assert q.trip_length == 14
    assert q.stops == 0


def test_token_never_appears_in_query_params():
    p, client = _provider(CHEAP_RESPONSE)
    p.cheap("COK", "GYD", "2026-11")
    _, params, headers = client.calls[0]
    assert "token" not in params
    assert headers["X-Access-Token"] == "fake-token"


def test_month_matrix_drops_zero_price_entries():
    p, client = _provider(MONTH_MATRIX_RESPONSE)
    quotes = p.month_matrix("COK", "GYD", "2026-11")
    assert len(quotes) == 1
    assert quotes[0].price_inr == 19200
    assert quotes[0].distance_km == 3050


def test_latest_parses_flat_array_with_round_trip_dates():
    p, client = _provider(LATEST_RESPONSE)
    quotes = p.latest("COK")
    assert len(quotes) == 1
    q = quotes[0]
    assert q.destination == "TBS"
    assert q.is_round_trip
    assert q.trip_length == 10


def test_parses_float_and_string_prices():
    payload = {
        "success": True,
        "data": [
            {
                "depart_date": "2026-11-05",
                "value": "15400.50",
                "distance": 3050,
            }
        ],
    }
    p, _ = _provider(payload)
    quotes = p.month_matrix("COK", "GYD", "2026-11")
    assert len(quotes) == 1
    assert quotes[0].price_inr == 15400


def test_cheap_handles_empty_list_payload_safely():
    payload = {"success": True, "data": []}
    p, _ = _provider(payload)
    quotes = p.cheap("COK", "GYD", "2026-11")
    assert quotes == []


def test_month_matrix_handles_dict_data_safely():
    payload = {"success": True, "data": {}}
    p, _ = _provider(payload)
    quotes = p.month_matrix("COK", "GYD", "2026-11")
    assert quotes == []


def test_retries_transient_server_error_and_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    class FlakyClient:
        def __init__(self):
            self.attempts = 0

        def get(self, url, params=None, headers=None):
            self.attempts += 1
            if self.attempts == 1:
                resp = _FakeResponse({})
                resp.status_code = 502
                return resp
            return _FakeResponse(CHEAP_RESPONSE)

    client = FlakyClient()
    p = TravelpayoutsProvider("fake-token", client=client)
    quotes = p.cheap("COK", "GYD", "2026-11", "2026-11")
    assert len(quotes) == 1
    assert client.attempts == 2


def test_retries_rate_limit_429_and_succeeds(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    class RateLimitedClient:
        def __init__(self):
            self.attempts = 0

        def get(self, url, params=None, headers=None):
            self.attempts += 1
            if self.attempts == 1:
                resp = _FakeResponse({})
                resp.status_code = 429
                resp.headers = {"Retry-After": "2"}
                return resp
            return _FakeResponse(CHEAP_RESPONSE)

    client = RateLimitedClient()
    p = TravelpayoutsProvider("fake-token", client=client)
    quotes = p.cheap("COK", "GYD", "2026-11", "2026-11")
    assert len(quotes) == 1
    assert client.attempts == 2
    assert 2.0 in slept


def test_max_calls_budget_enforced_during_retries(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    class AlwaysFailingClient:
        def __init__(self):
            self.attempts = 0

        def get(self, url, params=None, headers=None):
            self.attempts += 1
            resp = _FakeResponse({})
            resp.status_code = 502
            return resp

    client = AlwaysFailingClient()
    p = TravelpayoutsProvider("fake-token", client=client, max_calls=2)
    with pytest.raises(ProviderError, match="call budget of 2 exhausted"):
        p.cheap("COK", "GYD", "2026-11", "2026-11")
    assert p.calls_made == 2
