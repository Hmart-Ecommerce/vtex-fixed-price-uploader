"""Prove the writes landed.

HTTP 200 from the write endpoint proves nothing - VTEX answers 200 for a trade
policy that does not exist. Only reading the array back is evidence.

The Pricing API settles in roughly two minutes, so the caller waits before
reading. Reading immediately produces false failures.

A read-back that does not answer 200 splits in two, and the split is the whole
point of this module:

  404  the SKU has NO price row at all, after a write that answered 200. That
       is the phantom-200 case itself - a CONFIRMED write failure, counted in
       `confirmed_empty`. It needs a re-write, not a re-read.
  else `reader.is_failed_read` - 429, the network sentinel 0, an unexpected
       5xx. Nothing was learned; the pair's state in VTEX is UNKNOWN. Counted
       in `unreadable`, an OPEN QUESTION that the operator settles by running
       the verification again.

Folding the two together would report every missing price row as "could not
look", which hides exactly the failure the read-back exists to find.
"""

import time as time_module
from dataclasses import dataclass
from datetime import datetime, timezone

from vtex_fixed_price_uploader import reader
from vtex_fixed_price_uploader.money import money
from vtex_fixed_price_uploader.pricing import fetch_prices, is_live, policy1

EMPTY_DETAIL = ("FAILURE: the write returned 200 but the SKU has no price row "
                "on read-back. The price did not land - write it again.")
UNREADABLE_DETAIL = ("OPEN QUESTION: the read-back could not be completed, so "
                     "this pair's state in VTEX is unknown. Nothing is proven "
                     "either way - re-run the verification.")


@dataclass(frozen=True)
class VerifyResult:
    matched: int = 0
    mismatched: int = 0
    confirmed_empty: int = 0
    unreadable: int = 0
    still_multiple: int = 0
    rows: tuple = ()


def _sort_key(item):
    """Order a comparable() tuple without ever comparing None to a real value.

    Each field becomes (0,) when None and (1, value) otherwise. Python stops at
    the marker when two markers differ, so a None field is never held up
    against a float - which is what raised TypeError and aborted an entire
    verification pass once, on rows where two entries tied on value and one had
    no listPrice.

    Equality still distinguishes None from a real value, because it compares
    the tuple, not this key.
    """
    return tuple((0,) if field_value is None else (1, field_value)
                 for field_value in item)


def _bound(value):
    """One dateRange bound as a normalised string, or None.

    This package emits `+00:00`; VTEX stores `Z`. They spell the same instant,
    so comparing the raw strings reports a mismatch on every dated entry.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def comparable(entries):
    """An order-independent, date-normalised signature of a policy-1 array."""
    signature = []
    for entry in entries or []:
        if str(entry.get("tradePolicyId", "1")) != "1":
            continue
        starts, ends = (entry.get("dateRange") or {}).get("from"), \
            (entry.get("dateRange") or {}).get("to")
        min_qty = entry.get("minQuantity")
        signature.append((
            money(entry.get("value")),
            money(entry.get("listPrice")),
            1 if min_qty in (None, 0) else min_qty,
            _bound(starts),
            _bound(ends),
        ))
    return tuple(sorted(signature, key=_sort_key))


def verify(config, pre, token, wait=120, fetch=None, sleep=None):
    """Re-read every written pair and compare it to what was intended."""
    fetch = fetch or fetch_prices
    sleep = sleep or time_module.sleep
    if wait:
        sleep(wait)

    matched = mismatched = confirmed_empty = unreadable = still_multiple = 0
    rows = []
    now = datetime.now(timezone.utc)

    for sku, account in sorted(pre.write_pairs):
        expected = pre.compositions[(sku, account)].new_array
        status, data = fetch(account, sku, token)
        if reader.is_failed_read(status):
            unreadable += 1
            rows.append({"sku": sku, "account": account, "verdict": "unreadable",
                         "status": status, "detail": UNREADABLE_DETAIL})
            continue
        if data is None:
            confirmed_empty += 1
            rows.append({"sku": sku, "account": account,
                         "verdict": "confirmed_empty", "status": status,
                         "detail": EMPTY_DETAIL})
            continue

        actual = policy1(data)
        agrees = comparable(actual) == comparable(expected)
        live_count = sum(1 for e in actual if is_live(e, now))
        if live_count > 1:
            still_multiple += 1

        if agrees:
            matched += 1
        else:
            mismatched += 1
        rows.append({"sku": sku, "account": account,
                     "verdict": "match" if agrees else "mismatch",
                     "live_entries": live_count})

    return VerifyResult(matched=matched, mismatched=mismatched,
                        confirmed_empty=confirmed_empty,
                        unreadable=unreadable, still_multiple=still_multiple,
                        rows=tuple(rows))
