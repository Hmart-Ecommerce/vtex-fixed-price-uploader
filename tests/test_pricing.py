from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest

from vtex_fixed_price_uploader import pricing

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def entry(value, start=None, end=None, min_qty=1, policy="1", list_price=None):
    e = {"value": value, "listPrice": list_price, "minQuantity": min_qty,
         "tradePolicyId": policy}
    if start or end:
        e["dateRange"] = {"from": start, "to": end}
    return e


def test_parse_dt_handles_z_suffix():
    assert pricing.parse_dt("2026-08-14T04:00:00Z") == datetime(
        2026, 8, 14, 4, 0, tzinfo=timezone.utc)


def test_parse_dt_handles_offset_suffix():
    assert pricing.parse_dt("2026-08-14T04:00:00+00:00") == datetime(
        2026, 8, 14, 4, 0, tzinfo=timezone.utc)


def test_parse_dt_blank_is_none():
    assert pricing.parse_dt(None) is None
    assert pricing.parse_dt("") is None


def test_parse_dt_documents_and_applies_naive_timestamp_as_utc_rule():
    assert "Naive timestamps are assumed to be UTC" in pricing.parse_dt.__doc__
    assert pricing.parse_dt("2026-08-14T04:00:00") == datetime(
        2026, 8, 14, 4, 0, tzinfo=timezone.utc)


def test_parse_dt_only_replaces_a_terminal_z():
    with pytest.raises(ValueError) as caught:
        pricing.parse_dt("2026-08-14T04:00:00ZZZ")
    assert "ZZ+00:00" in str(caught.value)


@pytest.mark.parametrize("value", [20260814, 2026.0814, object()])
def test_parse_dt_returns_none_for_non_string_non_datetime(value):
    assert pricing.parse_dt(value) is None


def test_policy1_filters_other_policies():
    data = {"fixedPrices": [entry(1.0), entry(2.0, policy="2")]}
    assert [e["value"] for e in pricing.policy1(data)] == [1.0]


def test_policy1_accepts_integer_policy_id_one():
    data = {"fixedPrices": [entry(1.0, policy=1)]}
    assert pricing.policy1(data) == [entry(1.0, policy=1)]


def test_policy1_on_empty_payload():
    assert pricing.policy1(None) == []
    assert pricing.policy1({}) == []


def test_policy1_skips_non_dict_fixed_price_elements():
    data = {"fixedPrices": [None, entry(1.0), "invalid", 3]}
    assert pricing.policy1(data) == [entry(1.0)]


@pytest.mark.parametrize("date_range", [
    {"from": "2026-08-01T00:00:00Z", "to": None},
    {"from": None, "to": "2026-09-01T00:00:00Z"},
    {"from": None, "to": None},
])
def test_is_live_open_ended_entry_is_live(date_range):
    candidate = entry(1.0)
    candidate["dateRange"] = date_range
    assert pricing.is_live(candidate, NOW) is True


def test_is_live_future_entry_is_not():
    assert pricing.is_live(
        entry(1.0, start="2026-09-01T00:00:00Z"), NOW) is False


def test_is_live_expired_entry_is_not():
    assert pricing.is_live(
        entry(1.0, end="2026-08-01T00:00:00Z"), NOW) is False


def test_is_live_window_start_boundary_is_inclusive_and_documented():
    at_start = entry(1.0, start="2026-08-26T12:00:00Z")
    assert pricing.is_live(at_start, NOW) is True
    assert "inclusive" in pricing.is_live.__doc__.lower()


def test_is_live_window_end_boundary_is_inclusive_and_documented():
    at_end = entry(1.0, end="2026-08-26T12:00:00Z")
    assert pricing.is_live(at_end, NOW) is True
    assert "inclusive" in pricing.is_live.__doc__.lower()


def test_is_live_rejects_naive_now_before_inspecting_window():
    naive_now = datetime(2026, 8, 26, 12, 0)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        pricing.is_live(entry(1.0), naive_now)


def test_is_live_wholesale_tier_is_not_live_for_pricing_purposes():
    assert pricing.is_live(entry(1.0, min_qty=6), NOW) is False


@pytest.mark.parametrize(("min_qty", "expected"), [
    (None, True),
    (0, True),
    (1, True),
    ("1", True),
    ("2", False),
    (2, False),
    (1.0, True),
    (True, False),
    (False, False),
])
def test_is_live_coerces_single_unit_min_quantity_but_rejects_bool(
        min_qty, expected):
    assert pricing.is_live(entry(1.0, min_qty=min_qty), NOW) is expected


def test_entry_window_returns_parsed_bounds():
    candidate = entry(
        1.0,
        start="2026-08-01T00:00:00Z",
        end="2026-09-01T00:00:00Z",
    )
    assert pricing.entry_window(candidate) == (
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def test_live_entries_filters_policy_window_and_tier():
    live = entry(9.99)
    data = {"fixedPrices": [
        live,
        entry(8.99, policy="2"),
        entry(7.99, min_qty=6),
        entry(6.99, end="2026-08-01T00:00:00Z"),
    ]}
    assert pricing.live_entries(data, NOW) == [live]


def test_serving_today_is_the_lowest_live_value():
    data = {"basePrice": 12.99, "fixedPrices": [
        entry(9.99), entry(7.99), entry(4.99, end="2026-08-01T00:00:00Z")]}
    assert pricing.serving_today(data, NOW) == 7.99


def test_serving_today_falls_back_to_base_when_nothing_is_live():
    data = {"basePrice": 12.99, "fixedPrices": [
        entry(4.99, end="2026-08-01T00:00:00Z")]}
    assert pricing.serving_today(data, NOW) == 12.99


def test_serving_today_ignores_wholesale_tiers():
    data = {"basePrice": 12.99, "fixedPrices": [entry(3.00, min_qty=6)]}
    assert pricing.serving_today(data, NOW) == 12.99


def test_serving_today_ignores_non_numeric_live_values():
    data = {"basePrice": 12.99, "fixedPrices": [entry(""), entry("invalid")]}
    assert pricing.serving_today(data, NOW) == 12.99


def test_serving_today_is_none_without_base_price():
    data = {"fixedPrices": [entry(
        4.99, end="2026-08-01T00:00:00Z")]}
    assert pricing.serving_today(data, NOW) is None


def test_serving_today_is_none_without_live_entries_and_null_base():
    assert pricing.serving_today(
        {"basePrice": None, "fixedPrices": []}, NOW) is None


def test_base_price_rounds():
    assert pricing.base_price({"basePrice": 8.990000001}) == 8.99


def test_base_price_is_none_for_non_numeric_value():
    assert pricing.base_price({"basePrice": "invalid"}) is None


def test_fetch_prices_returns_status_and_payload(monkeypatch):
    calls = []

    class FakeResponse:
        status = 200
        def read(self):
            return b'{"basePrice": 8.99, "fixedPrices": []}'
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return FakeResponse()

    monkeypatch.setattr(pricing.urllib.request, "urlopen", fake_urlopen)
    status, data = pricing.fetch_prices("acct_one", "111", "tok")
    assert status == 200
    assert data["basePrice"] == 8.99
    assert calls[0].endswith("/acct_one/pricing/prices/111")


def test_fetch_prices_returns_404_without_raising(monkeypatch):
    calls = []
    sleeps = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise pricing.urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(pricing.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pricing, "_sleep", sleeps.append)
    status, data = pricing.fetch_prices("acct_one", "111", "tok", retries=2)
    assert status == 404
    assert data is None
    assert len(calls) == 1
    assert sleeps == []


def test_fetch_prices_retries_500_with_expected_backoff(monkeypatch):
    calls = []
    sleeps = []

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"basePrice": 8.99, "fixedPrices": []}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) < 3:
            raise pricing.urllib.error.HTTPError(
                req.full_url, 500, "Server Error", {}, None)
        return FakeResponse()

    monkeypatch.setattr(pricing.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pricing, "_sleep", sleeps.append)

    status, data = pricing.fetch_prices(
        "acct_one", "111", "tok", retries=2)

    assert status == 200
    assert data["basePrice"] == 8.99
    assert len(calls) == 3
    assert sleeps == [5, 8]


def test_fetch_prices_retries_429_and_caps_retry_after(monkeypatch):
    calls = []
    sleeps = []

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"basePrice": 8.99, "fixedPrices": []}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise pricing.urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests",
                {"Retry-After": "45"}, None)
        return FakeResponse()

    monkeypatch.setattr(pricing.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pricing, "_sleep", sleeps.append)

    status, data = pricing.fetch_prices(
        "acct_one", "111", "tok", retries=1)

    assert status == 200
    assert data["basePrice"] == 8.99
    assert len(calls) == 2
    assert sleeps == [30]


def test_fetch_prices_ignores_http_date_retry_after(monkeypatch):
    calls = []
    sleeps = []

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"basePrice": 8.99, "fixedPrices": []}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise pricing.urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests",
                {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, None)
        return FakeResponse()

    monkeypatch.setattr(pricing.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pricing, "_sleep", sleeps.append)

    status, _ = pricing.fetch_prices("acct_one", "111", "tok", retries=1)

    assert status == 200
    assert len(calls) == 2
    assert sleeps == [5]


def test_fetch_prices_returns_429_after_retries_are_exhausted(monkeypatch):
    calls = []
    sleeps = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise pricing.urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(pricing.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pricing, "_sleep", sleeps.append)

    status, data = pricing.fetch_prices(
        "acct_one", "111", "tok", retries=2)

    assert (status, data) == (429, None)
    assert len(calls) == 3
    assert sleeps == [5, 8]


def test_fetch_prices_retries_bare_timeout_error(monkeypatch):
    calls = []
    sleeps = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise TimeoutError("response timed out")

    monkeypatch.setattr(pricing.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pricing, "_sleep", sleeps.append)

    result = pricing.fetch_prices("acct_one", "111", "tok", retries=1)

    assert result == (0, None)
    assert len(calls) == 2
    assert sleeps == [5]


def test_fetch_prices_retries_invalid_json_from_200_response(monkeypatch):
    calls = []
    sleeps = []

    class FakeResponse:
        status = 200

        def read(self):
            return b"<html>gateway error</html>"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return FakeResponse()

    monkeypatch.setattr(pricing.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pricing, "_sleep", sleeps.append)

    result = pricing.fetch_prices("acct_one", "111", "tok", retries=1)

    assert result == (0, None)
    assert len(calls) == 2
    assert sleeps == [5]


def test_fetch_prices_never_puts_the_token_in_the_url(monkeypatch):
    seen = {}

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"basePrice": 8.99, "fixedPrices": []}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        return FakeResponse()

    monkeypatch.setattr(pricing.urllib.request, "urlopen", fake_urlopen)
    pricing.fetch_prices("acct_one", "111", "sensitive-token", retries=0)
    assert urlsplit(seen["url"]).scheme == "https"
    assert "sensitive-token" not in seen["url"]
    assert seen["headers"]["Vtexidclientautcookie"] == "sensitive-token"


def test_pricing_public_interface_and_status_contract_are_documented():
    assert pricing.datetime is datetime
    assert pricing.NETWORK_FAILURE_STATUS == 0
    assert "NETWORK_FAILURE_STATUS" in pricing.__doc__

    fetch_doc = pricing.fetch_prices.__doc__
    for meaning in ("401", "404", "429", "5xx", "network"):
        assert meaning in fetch_doc
    assert "three requests" in fetch_doc

    assert pricing.parse_dt.__annotations__ == {
        "value": str | datetime | None,
        "return": datetime | None,
    }
    assert pricing.policy1.__annotations__ == {
        "data": dict | None,
        "return": list[dict],
    }
    assert pricing.entry_window.__annotations__ == {
        "entry": dict,
        "return": tuple[datetime | None, datetime | None],
    }
    assert pricing.is_live.__annotations__ == {
        "entry": dict,
        "now": datetime,
        "return": bool,
    }
    assert pricing.live_entries.__annotations__ == {
        "data": dict | None,
        "now": datetime,
        "return": list[dict],
    }
    assert pricing.base_price.__annotations__ == {
        "data": dict | None,
        "return": float | None,
    }
    assert pricing.serving_today.__annotations__ == {
        "data": dict | None,
        "now": datetime,
        "return": float | None,
    }
    assert pricing.fetch_prices.__annotations__ == {
        "account": str,
        "sku": str,
        "token": str,
        "timeout": int,
        "retries": int,
        "return": tuple[int, dict | None],
    }
