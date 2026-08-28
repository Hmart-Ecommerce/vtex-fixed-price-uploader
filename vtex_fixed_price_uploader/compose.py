"""Build the array that replaces trade policy 1 for one SKU in one account.

POST /{account}/pricing/prices/{sku}/fixed/1 replaces the entire policy-1
array, so this tool never issues a delete. It computes the array that should
exist and posts that. One call removes and creates at once, so there is no
moment where the SKU carries no price.

This is spec section 7. Change it only against the spec.
"""

import math
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

from vtex_fixed_price_uploader.money import money
from vtex_fixed_price_uploader.parser import Row
from vtex_fixed_price_uploader.pricing import entry_window, policy1


@dataclass(frozen=True)
class Composition:
    """The outcome of one composition, isolated from the caller's payload.

    The sequences are tuples and the entries are deep copies, so neither side
    can reach through the result and change the other. `frozen=True` alone only
    stops rebinding the attributes; it says nothing about what they hold, and
    this is the one module where an accidental mutation writes wrong prices.
    """

    new_array: tuple[dict, ...] = ()
    dropped: tuple[dict, ...] = ()
    kept: tuple[dict, ...] = ()
    unrecognised: tuple[dict, ...] = ()


def _policy_number(value: object) -> int | None:
    """The tradePolicyId as an integer, or None when it is not one.

    VTEX returns ints, so this is a low-likelihood path. It exists because the
    failure mode is silent price deletion: `policy1` matches on
    `str(tradePolicyId) == "1"`, so `1.0` or `"01"` matches nothing, lands in
    neither kept nor dropped, and is erased by the replacing write.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number != int(number):
        return None
    return int(number)


def unrecognised_entries(data: dict | None) -> list[dict]:
    """Policy-1 entries that `policy1` cannot see, and so would silently erase.

    An id that parses to an integer other than 1 is a genuine other-policy
    entry; it is another team's business and is left alone. Everything else
    that `policy1` skipped - an absent id, an unparseable one, or one that
    means 1 without spelling it `1` - is refused rather than deleted.
    """
    found = []
    for original in (data or {}).get("fixedPrices") or []:
        if not isinstance(original, dict):
            continue
        policy = original.get("tradePolicyId")
        if str(policy) == "1":
            continue
        number = _policy_number(policy)
        if number is None or number == 1:
            found.append(deepcopy(original))
    return found


def _require_aware(now: datetime | None) -> None:
    """`now` must carry a UTC offset.

    Same guard and same wording as `pricing.is_live`. Without it a naive `now`
    dies deep in a datetime comparison with a TypeError that names neither the
    argument nor the caller at fault.
    """
    if now is None or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")


def expired(entry: dict, now: datetime) -> bool:
    """The entry has an end bound and it is already past."""
    _require_aware(now)
    _, ends = entry_window(entry)
    return ends is not None and ends <= now


def overlaps(entry: dict, start: datetime | None,
             end: datetime | None) -> bool:
    """The entry's window intersects [start, end].

    A missing bound is open, and an open bound intersects everything on that
    side. An entry with no end bound therefore overlaps any window that begins
    after it starts - which is why open-ended entries are always removed. They
    are the worst offenders in practice: they never expire and they compete
    with every campaign that follows.
    """
    starts, ends = entry_window(entry)
    starts_before_window_ends = (starts is None or end is None or starts < end)
    ends_after_window_starts = (ends is None or start is None or ends > start)
    return starts_before_window_ends and ends_after_window_starts


def row_to_entry(row: Row) -> dict:
    """One CSV row as a VTEX fixedPrices entry.

    dateRange is emitted only when BOTH bounds exist. VTEX's schema requires
    `from` and `to` together, so a half-open window cannot be expressed - it
    becomes no window, which is open on both sides.
    """
    entry = {
        "value": money(row.promo),
        "listPrice": money(row.list_price),
        "minQuantity": 1,
    }
    if row.start and row.end:
        entry["dateRange"] = {
            "from": row.start.isoformat(),
            "to": row.end.isoformat(),
        }
    return entry


def compose(rows: Iterable[Row], data: dict | None,
            now: datetime) -> Composition:
    """The replacement array for one (sku, account) pair.

    new_array = the CSV's entries
              + existing entries that are a wholesale tier
              + existing entries that neither expired nor overlap the CSV

    `rows` must all belong to the same (sku, account) pair. `now` is fixed once
    per run by the caller so a long run cannot change its own answers mid-flight.
    """
    _require_aware(now)
    rows = list(rows)
    csv_entries: list[dict] = [row_to_entry(r) for r in rows]
    windows: Sequence[tuple[datetime | None, datetime | None]] = [
        (r.start, r.end) for r in rows]

    kept: list[dict] = []
    dropped: list[dict] = []
    for original in policy1(data):
        entry = deepcopy(original)
        min_quantity = entry.get("minQuantity")
        try:
            wholesale = (not isinstance(min_quantity, bool)
                         and float(min_quantity) >= 2)
        except (TypeError, ValueError):
            wholesale = False
        if wholesale:
            kept.append(entry)          # wholesale tier, never our business
            continue
        if expired(entry, now):
            dropped.append(entry)
            continue
        if any(overlaps(entry, start, end) for start, end in windows):
            dropped.append(entry)
            continue
        kept.append(entry)

    return Composition(new_array=tuple(csv_entries + kept),
                       dropped=tuple(dropped), kept=tuple(kept),
                       unrecognised=tuple(unrecognised_entries(data)))
