"""The sixteen checks that run before anything is written.

Eight blocking rules remove a row from the batch; the operator cannot consent
past them. Seven warning rules are shown and acknowledged per group. One
informational rule is reported and needs no action.

Every rule here was drawn from a defect observed in production, not from
theory. Messages are operator-facing English and avoid rule ids on purpose -
ids appear only in the downloadable CSV.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from vtex_fixed_price_uploader.compose import (
    Composition, expired, overlaps, row_to_entry)
from vtex_fixed_price_uploader.config import Config
from vtex_fixed_price_uploader.money import money, same
from vtex_fixed_price_uploader.parser import Row
from vtex_fixed_price_uploader.pricing import (
    base_price, entry_window, serving_today)
from vtex_fixed_price_uploader.reader import is_failed_read
from vtex_fixed_price_uploader.writer import _SKU_PATTERN

MAX_PRICE = 999.0
DEEP_DISCOUNT_FLOOR = 0.40   # promo below 40% of base is >60% off

# Severity is the safety contract, not a display hint: `blocked_pairs()` reads
# it to decide which rows never reach the writer. Every id here is pinned by
# test_every_rule_severity_is_pinned_by_id so a careless edit cannot downgrade
# a blocking rule or promote a warning in silence.
#
# W6 (campaign collision) is a WARNING deliberately. Spec sections 9 and 16
# record the decision: section 7 resolves the collision structurally, and W6
# exists to make it visible, not to prevent it. Review asked for it to be
# promoted to blocking and the project declined. Do not "correct" it.
SEVERITY = {
    "B1": "blocking", "B2": "blocking", "B3": "blocking",
    "B4": "blocking", "B5": "blocking", "B6": "blocking",
    "B7": "blocking", "B8": "blocking",
    "W1": "warning", "W2": "warning", "W3": "warning",
    "W4": "warning", "W5": "warning", "W6": "warning",
    "W7": "warning",
    "I1": "info",
}


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    sku: str
    code: str
    account: str
    message: str
    detail: str = ""


def _finding(rule: str, row_or_key: tuple[str, str, str], message: str,
             detail: str = "") -> Finding:
    sku, code, account = row_or_key
    return Finding(rule=rule, severity=SEVERITY[rule], sku=sku, code=code,
                   account=account, message=message, detail=detail)


def _money_str(value: float | None) -> str:
    return "n/a" if value is None else "${:,.2f}".format(value)


def _row_window(row: Row) -> tuple[datetime | None, datetime | None]:
    """The window this row will actually carry once written.

    `compose.row_to_entry` emits a dateRange only when BOTH bounds exist -
    VTEX's schema cannot express a half-open window - so a row missing either
    bound is written fully open. Every window comparison in this module
    normalises through here, so no rule can compare one row's raw bounds
    against another row's collapsed ones.
    """
    if row.start and row.end:
        return row.start, row.end
    return None, None


def _blocking_for_row(row: Row, status: int) -> list[Finding]:
    """Every blocking finding for one row except B1, which needs its siblings.

    A failed read (B6) and a product absent from the region (B4) are separate
    rules on purpose - see the comment at the B6 branch below.
    """
    key = (row.sku, row.code, row.account)
    out = []

    if not isinstance(row.sku, str) or not _SKU_PATTERN.match(row.sku):
        out.append(_finding(
            "B7", key,
            "SKU {!r} is not a plain identifier.".format(row.sku),
            "The sku column must contain only a plain identifier. A value "
            "such as 1234.0 is a spreadsheet formatting artifact; format "
            "the sku column as text and correct the value before uploading."))

    if is_failed_read(status):
        # Nothing was learned about this pair, so nothing about it can be
        # validated. An empty verdict on an unread row is worse than a wrong
        # one: the row would sail through to the writer unchecked.
        #
        # This is B6, not B4. The rule id is what lands in the downloadable
        # CSV, and the two conditions ask the operator for opposite things:
        # B4 says take the row out of the file, B6 says put it back through
        # unchanged. Collapsing them into one id is the same defect the
        # reader fixed one layer down, where a throttled read and a genuine
        # "no price row" came back in the same shape.
        out.append(_finding(
            "B6", key,
            "The current price could not be read for this region.",
            "Nothing is wrong with this product - the price service did not "
            "answer. Re-run the file to check this row."))
    elif status == 404:
        out.append(_finding(
            "B4", key,
            "This product does not exist in that region.",
            "Remove this region from your file, or have the product added to "
            "that region's catalog before uploading."))

    for label, value, column in (
            ("price", row.promo, "promoPrice" + row.code),
            ("crossed-out price", row.list_price, "listPrice" + row.code)):
        if value is None:
            continue
        if value <= 0 or value > MAX_PRICE:
            out.append(_finding(
                "B5", key,
                "This {} looks wrong.".format(label),
                "Line {}, column {}: {} is outside the accepted range of "
                "$0.01 to {}. Correct the price before uploading.".format(
                    row.line, column, _money_str(value),
                    _money_str(MAX_PRICE))))

    if row.start and row.end and row.end <= row.start:
        out.append(_finding(
            "B2", key,
            "The end date is before the start date.",
            "Line {}: {} to {}. Correct the dates so the end is after the "
            "start.".format(row.line, row.start.date(), row.end.date())))

    return out


def _b3(row: Row, now: datetime) -> list[Finding]:
    key = (row.sku, row.code, row.account)
    if row.end and row.end <= now:
        return [_finding("B3", key, "These dates have already passed.",
                         "Line {}: the promotion ended {}. Update the dates "
                         "or remove this row before uploading.".format(
                             row.line, row.end.date()))]
    return []


def _b8(row: Row, composition: Composition | None) -> list[Finding]:
    """Refuse a replacing write when stored entries cannot be classified."""
    if composition is None or not composition.unrecognised:
        return []
    key = (row.sku, row.code, row.account)
    return [_finding(
        "B8", key,
        "Stored pricing contains an entry this tool cannot classify.",
        "This price will not be written. Have a human review the stored "
        "fixed-price entries for this SKU and account before uploading.")]


def _b1(rows: Sequence[Row]) -> list[Finding]:
    """Same pair listed twice with windows that intersect.

    Every colliding combination is reported, not just the first one found. A
    pair on three overlapping lines is three decisions for the operator, and
    reporting one of them at a time costs a re-run per collision.
    """
    out: list[Finding] = []
    seen: dict[tuple[str, str], list[Row]] = {}
    for row in rows:
        seen.setdefault((row.sku, row.account), []).append(row)
    for (sku, account), group in seen.items():
        if len(group) < 2:
            continue
        for i, first in enumerate(group):
            for second in group[i + 1:]:
                if not overlaps(row_to_entry(first), *_row_window(second)):
                    continue
                out.append(_finding(
                    "B1", (sku, first.code, account),
                    "This product appears more than once in your file for "
                    "overlapping dates.",
                    "Lines {} and {}: {} and {}. Pick one.".format(
                        first.line, second.line,
                        _money_str(first.promo), _money_str(second.promo))))
    return out


def _warnings_for_row(row: Row, data: dict | None, base: float,
                      now: datetime) -> list[Finding]:
    key = (row.sku, row.code, row.account)
    out = []
    raised_against_base = False

    if same(row.promo, base):
        out.append(_finding(
            "W2", key,
            "No discount - this is the same as the shelf price.",
            "Both are {}.".format(_money_str(base))))
    elif row.promo > base:
        raised_against_base = True
        out.append(_finding(
            "W1", key,
            "Your price is higher than the shelf price. This is a price "
            "increase, not a promotion - it will expire and the price "
            "will drop back on its own.",
            "Promotion {} against a shelf price of {}.".format(
                _money_str(row.promo), _money_str(base))))

    if row.list_price is not None and not same(row.list_price, base):
        out.append(_finding(
            "W3", key,
            "The crossed-out price does not match the real shelf price.",
            "Your file says {}; the shelf price is {}.".format(
                _money_str(row.list_price), _money_str(base))))

    # Rounded on both sides: 3.00 * 0.40 is 1.2000000000000002 in binary
    # floating point, which would fire at exactly the 60% the spec excludes.
    if money(row.promo) < money(base * DEEP_DISCOUNT_FLOOR):
        out.append(_finding(
            "W4", key,
            "Unusually deep discount - check for a typo.",
            "Sets a price of {} against a shelf price of {}.".format(
                _money_str(row.promo), _money_str(base))))

    serving = serving_today(data, now)
    # W5 restates W1 whenever nothing is live, because `serving_today` falls
    # back to the base price. One fact, one acknowledgement group.
    if (not raised_against_base and serving is not None
            and row.promo > serving and not same(row.promo, serving)):
        out.append(_finding(
            "W5", key,
            "Shoppers pay less today; this raises the price.",
            "Currently {}, this sets {}.".format(
                _money_str(serving), _money_str(row.promo))))

    return out


def _w6(pair_rows: Sequence[Row], composition: Composition | None,
        now: datetime) -> list[Finding]:
    """Live entries this upload ends earlier than the CSV keeps them running.

    Compared against the LATEST end across every CSV row for the pair, not one
    row's own end: two consecutive rows that carry the pair through October do
    not cut short an entry ending in October. A CSV row with no usable window
    is written unbounded and therefore covers everything - nothing ends after
    an unbounded end. Computed once per pair so one dropped entry cannot
    render two identical checkboxes for one decision.
    """
    if not pair_rows or composition is None:
        return []

    ends = [_row_window(r)[1] for r in pair_rows]
    if any(end is None for end in ends):
        return []
    latest_end = max(ends)

    first = pair_rows[0]
    key = (first.sku, first.code, first.account)
    out: list[Finding] = []
    seen: set[Finding] = set()
    for dropped in composition.dropped:
        if expired(dropped, now):
            continue
        _, ends_at = entry_window(dropped)
        if ends_at is not None and ends_at <= latest_end:
            continue
        until = ("with no end date" if ends_at is None
                 else "until {}".format(ends_at.date()))
        finding = _finding(
            "W6", key,
            "Uploading this ends a promotion that was scheduled to keep "
            "running.",
            "{} was set to run {}.".format(
                _money_str(money(dropped.get("value"))), until))
        if finding in seen:
            continue
        seen.add(finding)
        out.append(finding)
    return out


def _w7(pair_rows: Sequence[Row], composition: Composition | None,
        base: float | None, now: datetime) -> list[Finding]:
    """Days a dropped entry was covering that the new campaign does not reach.

    W6 asks "does this end a campaign that would have run AFTER mine?". This
    asks the mirror question: "does this leave a hole BEFORE mine starts?".
    Observed live in QA - a campaign running Aug 28 to Sep 03 was replaced by
    one running Sep 01 to Sep 10. Composition was right and W6 was correctly
    silent, because nothing survived past the new campaign; yet four days
    silently went back to full price. To the operator both are the same
    surprise, so both are warnings.

    Compared against the EARLIEST start across every CSV row for the pair, the
    mirror of W6's latest end. A CSV row with no usable window is written
    unbounded and so covers everything before it - it can never leave a gap.
    Computed once per pair so one dropped entry cannot render two identical
    checkboxes for one decision.
    """
    if not pair_rows or composition is None:
        return []

    starts = [_row_window(r)[0] for r in pair_rows]
    if any(start is None for start in starts):
        return []
    csv_start = min(starts)

    first = pair_rows[0]
    key = (first.sku, first.code, first.account)
    out: list[Finding] = []
    seen: set[Finding] = set()
    for dropped in composition.dropped:
        # An expired entry is covering nothing today, so removing it loses
        # nothing. Days already past cannot surprise anyone either, which is
        # why the gap is clamped to `now` rather than read off the entry.
        if expired(dropped, now):
            continue
        starts_at, ends_at = entry_window(dropped)
        gap_start = now if starts_at is None else max(starts_at, now)
        gap_end = csv_start if ends_at is None else min(ends_at, csv_start)
        if gap_start >= gap_end:
            continue
        price = "" if base is None else " of {}".format(_money_str(base))
        finding = _finding(
            "W7", key,
            "Uploading this leaves {} to {} with no promotion - the product "
            "goes back to its normal price{} on those days.".format(
                gap_start.date(), gap_end.date(), price),
            "The promotion being replaced was covering those days and your "
            "file does not start until {}. Move your start date to {} or add "
            "a row covering the days in between.".format(
                csv_start.date(), gap_start.date()))
        if finding in seen:
            continue
        seen.add(finding)
        out.append(finding)
    return out


def evaluate(rows: Sequence[Row],
             reads: Mapping[tuple[str, str], tuple[int, dict | None]],
             compositions: Mapping[tuple[str, str], Composition],
             config: Config,
             now: datetime) -> list[Finding]:
    """Every finding for the whole batch, in a stable order.

    `reads` maps (sku, account) to (http_status, payload). It must cover every
    configured account for every SKU in the sheet, not only the pairs the sheet
    names - rule I1 reports a region the sheet left blank where the product
    does exist.

    Warnings are suppressed only when the read produced no base price to
    compare against. Being blocked is not itself a reason to hide them: a row
    blocked on its dates was still read successfully, and its price typo is
    knowable on run one.
    """
    findings = list(_b1(rows))
    ordered = sorted(rows, key=lambda r: (r.sku, r.account, r.line))
    checked_compositions: set[tuple[str, str]] = set()
    for row in ordered:
        status, data = reads.get((row.sku, row.account), (0, None))
        findings.extend(_blocking_for_row(row, status))
        findings.extend(_b3(row, now))
        key = (row.sku, row.account)
        if key not in checked_compositions:
            checked_compositions.add(key)
            findings.extend(_b8(row, compositions.get(key)))

    by_pair: dict[tuple[str, str], list[Row]] = {}
    for row in ordered:
        by_pair.setdefault((row.sku, row.account), []).append(row)

    done: set[tuple[str, str]] = set()
    for row in ordered:
        key = (row.sku, row.account)
        _, data = reads.get(key, (0, None))
        base = base_price(data)
        if base is None:
            continue
        findings.extend(_warnings_for_row(row, data, base, now))
        if key not in done:
            done.add(key)
            findings.extend(_w6(by_pair[key], compositions.get(key), now))
            findings.extend(
                _w7(by_pair[key], compositions.get(key), base, now))

    covered = {(r.sku, r.code) for r in rows}
    codes_by_account: dict[str, list[str]] = {}
    for code, account in config.accounts.items():
        codes_by_account.setdefault(account, []).append(code)
    for (sku, account), (status, _data) in sorted(reads.items()):
        if status != 200:
            continue
        for code in codes_by_account.get(account, [""]):
            if (sku, code) not in covered:
                findings.append(_finding(
                    "I1", (sku, code, account),
                    "No price given for this region.",
                    "The product exists here but your file left it blank."))

    return findings


def blocked_pairs(findings: Sequence[Finding]) -> set[tuple[str, str]]:
    """(sku, account) pairs the runner must exclude from the batch."""
    return {(f.sku, f.account) for f in findings if f.severity == "blocking"}
