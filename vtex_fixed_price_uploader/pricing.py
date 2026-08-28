"""VTEX Pricing API reads.

GET /{account}/pricing/prices/{sku} returns basePrice and the whole fixedPrices
array in one call. There is no batch read. A 404 means the SKU has no price row
in that account - common, and not an error.

NETWORK_FAILURE_STATUS is the status 0 sentinel for exhausted transport or
response-phase failures where no HTTP status is available.
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from vtex_fixed_price_uploader.money import money

PRICING_HOST = "https://api.vtex.com"
NETWORK_FAILURE_STATUS = 0
_sleep = time.sleep


def parse_dt(value: str | datetime | None) -> datetime | None:
    """A VTEX timestamp as a UTC instant. None for null or blank.

    VTEX stores a `Z` suffix; this package emits `+00:00`. Both spell the same
    instant, so everything is normalised to UTC here and compared as datetimes,
    never as raw strings. Naive timestamps are assumed to be UTC because VTEX
    timestamps are defined as UTC even when the suffix is omitted.
    """
    if not isinstance(value, (str, datetime)):
        return None
    if value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        timestamp = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def policy1(data: dict | None) -> list[dict]:
    """The raw policy-1 entries, in API array order."""
    return [e for e in (data or {}).get("fixedPrices") or []
            if isinstance(e, dict)
            and str(e.get("tradePolicyId")) == "1"]


def entry_window(
        entry: dict) -> tuple[datetime | None, datetime | None]:
    date_range = entry.get("dateRange") or {}
    return parse_dt(date_range.get("from")), parse_dt(date_range.get("to"))


def is_live(entry: dict, now: datetime) -> bool:
    """In window at `now`, and not a wholesale tier.

    Both window boundaries are inclusive. When one entry ends exactly when its
    replacement starts, both are live at that instant.

    minQuantity above 1 prices a different thing - a case, not a unit - so it
    never competes for the single-unit price and must not be read as "what is
    live".
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    min_quantity = entry.get("minQuantity")
    if isinstance(min_quantity, bool):
        return False
    if min_quantity is not None:
        try:
            if float(min_quantity) not in (0, 1):
                return False
        except (TypeError, ValueError):
            return False
    starts, ends = entry_window(entry)
    if starts and now < starts:
        return False
    if ends and now > ends:
        return False
    return True


def live_entries(data: dict | None, now: datetime) -> list[dict]:
    return [e for e in policy1(data) if is_live(e, now)]


def _number(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def base_price(data: dict | None) -> float | None:
    return money(_number((data or {}).get("basePrice")))


def serving_today(data: dict | None, now: datetime) -> float | None:
    """The lowest live policy-1 value, else the base price.

    This is the FIXED-PRICE layer, not the charged price. Cart-level promotions
    can take the shopper lower still. Never present this as what a customer pays.
    """
    values = [money(_number(e.get("value")))
              for e in live_entries(data, now)]
    values = [v for v in values if v is not None]
    if values:
        return min(values)
    return base_price(data)


def fetch_prices(
        account: str, sku: str, token: str, timeout: int = 30,
        retries: int = 2) -> tuple[int, dict | None]:
    """Return ``(http_status, payload)``; payload is None for any non-200.

    A None payload can mean 401 bad token, 404 no price row, 429 throttled,
    exhausted 5xx responses, or NETWORK_FAILURE_STATUS (0) when the network or
    response body failed. Callers must inspect the status. A 404 is ordinary:
    roughly one in ten SKU-account pairs has no price row. ``retries=2`` means
    two retries after the first attempt, for up to three requests total.
    """
    url = "{}/{}/pricing/prices/{}".format(PRICING_HOST, account, sku)
    attempt = 0
    while True:
        retry_after = None
        req = urllib.request.Request(url, headers={
            "VtexIdclientAutCookie": token,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return resp.status, None
                return 200, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if (exc.code != 429 and exc.code < 500) or attempt >= retries:
                return exc.code, None
            if exc.code == 429 and exc.headers:
                try:
                    retry_after = max(
                        0, min(30, int(exc.headers.get("Retry-After"))))
                except (TypeError, ValueError):
                    pass
        except (urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError):
            if attempt >= retries:
                return NETWORK_FAILURE_STATUS, None
        attempt += 1
        _sleep(min(30, retry_after if retry_after is not None
                   else 2 + attempt * 3))
