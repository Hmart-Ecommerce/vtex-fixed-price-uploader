"""Wide sheet in, flat rows out.

The sheet is one row per SKU with four columns per region. A row explodes into
up to one Row per configured region code. A region whose promo price is blank
is skipped - that is how the sheet expresses "no promotion here", not an error.
"""

import csv
import io
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from vtex_fixed_price_uploader.money import money

_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{1,2}):(\d{2})(?::(\d{2}))?(.*)$")


@dataclass(frozen=True)
class Row:
    sku: str
    code: str
    account: str
    promo: float
    list_price: float | None
    start: datetime | None
    end: datetime | None
    promo_type: str
    line: int

    def __post_init__(self):
        object.__setattr__(self, "sku", str(self.sku))


def parse_sheet_datetime(value: str | None) -> datetime | None:
    """One sheet timestamp as a UTC instant, or None when blank.

    The sheet writes the hour without a leading zero (2026-08-28T1:00:00-03:00)
    and datetime.fromisoformat rejects that outright. Pad the hour, then let
    fromisoformat do the rest.

    The -03:00 offset is an artifact of the tool that produced the sheet and is
    NOT the region's real timezone, but it does spell the intended instant. Do
    not "correct" it - reinterpreting the offset would move every promotion by
    hours. A timestamp without an offset is rejected rather than assumed UTC.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _STAMP.match(text)
    if not match:
        raise ValueError("unrecognised date format: {!r}".format(value))
    day, hour, minute, second, offset = match.groups()
    normalised = "{}T{:02d}:{}:{}{}".format(
        day, int(hour), minute, second or "00", offset.replace("Z", "+00:00"))
    parsed = datetime.fromisoformat(normalised)
    if parsed.tzinfo is None:
        raise ValueError("timestamp {!r}: an offset is required".format(value))
    return parsed.astimezone(timezone.utc)


def _money_cell(value):
    """A price cell. Strips currency symbols and thousands separators."""
    if value is None:
        return None
    text = str(value).strip().replace("$", "")
    if not text:
        return None
    if (("." in text and "," in text and text.rfind(",") > text.find("."))
            or re.search(r",(?!\d{3}(?:,|\.|$))", text)):
        raise ValueError("ambiguous price value {!r}".format(value))
    return money(text.replace(",", ""))


def _as_text_stream(source):
    if isinstance(source, (str, os.PathLike)):
        with open(source, newline="", encoding="utf-8-sig") as stream:
            return io.StringIO(stream.read())
    if isinstance(source, io.TextIOBase):
        return io.StringIO(source.read().removeprefix("\ufeff"))
    data = source.read()
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    return io.StringIO(data)


def parse_csv(source, config) -> list[Row]:
    """Flat rows from a path, a text stream, or a bytes stream."""
    stream = _as_text_stream(source)
    reader = csv.DictReader(stream)
    rows = []
    for record in reader:
        line = reader.line_num
        sku = (record.get("skuId") or "").strip()
        if not sku:
            continue
        promo_type = (record.get("promo_type") or "").strip()

        def parse_cell(column, parser):
            try:
                return parser(record.get(column))
            except ValueError as error:
                raise ValueError(
                    "line {} SKU {!r} column {}: {}".format(
                        line, sku, column, error)
                ) from error

        for code, account in config.accounts.items():
            promo = parse_cell("promoPrice" + code, _money_cell)
            if promo is None:
                continue
            rows.append(Row(
                sku=sku,
                code=code,
                account=account,
                promo=promo,
                list_price=parse_cell("listPrice" + code, _money_cell),
                start=parse_cell("dateStart" + code, parse_sheet_datetime),
                end=parse_cell("dateEnd" + code, parse_sheet_datetime),
                promo_type=promo_type,
                line=line,
            ))
    return rows
