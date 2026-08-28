import io
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from vtex_fixed_price_uploader.config import load_config
from vtex_fixed_price_uploader.parser import Row, parse_csv, parse_sheet_datetime

CFG = load_config({"accounts": {"R1": "acct_one", "R2": "acct_two"},
                   "never_write": ["acct_master"], "trade_policy": "1"})

HEADER = ("skuId,SkuReferenceCode,"
          "listPriceR1,promoPriceR1,dateStartR1,dateEndR1,"
          "listPriceR2,promoPriceR2,dateStartR2,dateEndR2,"
          "promo_type,main_item\n")


def sheet(*lines):
    return io.StringIO(HEADER + "".join(l + "\n" for l in lines))


def test_single_digit_hour_is_parsed():
    got = parse_sheet_datetime("2026-08-28T1:00:00-03:00")
    assert got == datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)


def test_two_digit_hour_still_works():
    got = parse_sheet_datetime("2026-08-28T11:00:00-03:00")
    assert got == datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)


def test_z_suffix_is_parsed():
    got = parse_sheet_datetime("2026-08-28T04:00:00Z")
    assert got == datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)


def test_blank_datetime_is_none():
    assert parse_sheet_datetime("") is None
    assert parse_sheet_datetime(None) is None
    assert parse_sheet_datetime("   ") is None


def test_unparseable_datetime_raises():
    with pytest.raises(ValueError):
        parse_sheet_datetime("next tuesday")


def test_parser_functions_have_documented_annotations():
    assert parse_sheet_datetime.__annotations__ == {
        "value": str | None,
        "return": datetime | None,
    }
    assert parse_csv.__annotations__ == {"return": list[Row]}


def test_naive_datetime_requires_an_offset():
    value = "2026-08-28T1:00:00"

    with pytest.raises(ValueError) as error:
        parse_sheet_datetime(value)

    assert value in str(error.value)
    assert "offset is required" in str(error.value)


def test_one_row_explodes_into_one_row_per_region():
    rows = parse_csv(sheet(
        "111,,8.99,7.99,2026-08-28T1:00:00-03:00,2026-09-18T1:00:00-03:00,"
        "9.99,8.49,2026-08-28T4:00:00-03:00,2026-09-18T4:00:00-03:00,weekly,1"
    ), CFG)
    assert len(rows) == 2
    assert {r.code for r in rows} == {"R1", "R2"}
    r1 = next(r for r in rows if r.code == "R1")
    assert r1.sku == "111"
    assert r1.account == "acct_one"
    assert r1.promo == 7.99
    assert r1.list_price == 8.99
    assert r1.start == datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
    assert r1.end == datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)
    assert r1.promo_type == "weekly"
    assert r1.line == 2
    r2 = next(r for r in rows if r.code == "R2")
    assert r2.start == datetime(2026, 8, 28, 7, 0, tzinfo=timezone.utc)
    assert r2.end == datetime(2026, 9, 18, 7, 0, tzinfo=timezone.utc)


def test_blank_promotion_dates_are_none():
    rows = parse_csv(sheet(
        "111,,8.99,7.99,,,9.99,8.49,,,weekly,1"
    ), CFG)

    assert [(row.start, row.end) for row in rows] == [(None, None), (None, None)]


def test_blank_promo_price_skips_that_region_without_error():
    rows = parse_csv(sheet(
        "111,,8.99,7.99,2026-08-28T1:00:00-03:00,2026-09-18T1:00:00-03:00,"
        ",,,,weekly,1"
    ), CFG)
    assert [r.code for r in rows] == ["R1"]


def test_blank_promo_skips_malformed_dates_for_that_region():
    rows = parse_csv(sheet(
        "111,,8.99,7.99,2026-08-28T1:00:00-03:00,2026-09-18T1:00:00-03:00,"
        ",,not-a-date,also-not-a-date,weekly,1"
    ), CFG)

    assert [row.code for row in rows] == ["R1"]


def test_blank_sku_is_skipped():
    rows = parse_csv(sheet(
        ",,8.99,7.99,2026-08-28T1:00:00-03:00,2026-09-18T1:00:00-03:00,"
        ",,,,weekly,1"
    ), CFG)
    assert rows == []


def test_currency_symbols_and_thousands_separators_are_stripped():
    rows = parse_csv(sheet(
        "111,,\"$1,208.99\",\"$1,207.99\",2026-08-28T1:00:00-03:00,"
        "2026-09-18T1:00:00-03:00,,,,,weekly,1"
    ), CFG)
    assert rows[0].promo == 1207.99
    assert rows[0].list_price == 1208.99


def test_comma_decimal_price_is_rejected_as_ambiguous():
    value = "1.208,99"
    source = sheet(
        '111,,8.99,"{}",,,,,,,weekly,1'.format(value)
    )

    with pytest.raises(ValueError) as error:
        parse_csv(source, CFG)

    assert value in str(error.value)


def test_missing_region_columns_are_ignored_not_fatal():
    """A sheet exported without one region's columns still parses the rest."""
    header = ("skuId,listPriceR1,promoPriceR1,dateStartR1,dateEndR1,promo_type\n")
    src = io.StringIO(header + "111,8.99,7.99,2026-08-28T1:00:00-03:00,"
                               "2026-09-18T1:00:00-03:00,weekly\n")
    rows = parse_csv(src, CFG)
    assert [r.code for r in rows] == ["R1"]


def test_bytes_file_object_is_accepted():
    """Colab's upload widget hands over bytes, not text."""
    raw = (HEADER + "111,,8.99,7.99,2026-08-28T1:00:00-03:00,"
           "2026-09-18T1:00:00-03:00,,,,,weekly,1\n").encode("utf-8-sig")
    rows = parse_csv(io.BytesIO(raw), CFG)
    assert rows[0].promo == 7.99


def test_bom_text_file_matches_path_source(tmp_path):
    raw = (HEADER + "111,,8.99,7.99,2026-08-28T1:00:00-03:00,"
           "2026-09-18T1:00:00-03:00,,,,,weekly,1\n").encode("utf-8-sig")
    path = tmp_path / "prices.csv"
    path.write_bytes(raw)

    path_rows = parse_csv(path.as_posix(), CFG)
    with path.open(encoding="utf-8", newline="") as stream:
        text_rows = parse_csv(stream, CFG)

    assert text_rows == path_rows


@pytest.mark.filterwarnings("error::ResourceWarning")
def test_path_object_is_accepted(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text(
        HEADER + "111,,8.99,7.99,2026-08-28T1:00:00-03:00,"
        "2026-09-18T1:00:00-03:00,,,,,weekly,1\n",
        encoding="utf-8-sig",
    )

    rows = parse_csv(path, CFG)

    assert rows[0].promo == 7.99


def test_path_source_is_closed(monkeypatch):
    stream = io.StringIO(
        HEADER + "111,,8.99,7.99,2026-08-28T1:00:00-03:00,"
        "2026-09-18T1:00:00-03:00,,,,,weekly,1\n"
    )

    def fake_open(*args, **kwargs):
        return stream

    monkeypatch.setattr("builtins.open", fake_open)

    parse_csv("prices.csv", CFG)

    assert stream.closed


def test_bad_cell_error_names_file_line_sku_and_column():
    valid = "111,,8.99,7.99,,,,,,,weekly,1"
    bad = "bad-sku,,8.99,abc,,,,,,,weekly,1"

    with pytest.raises(ValueError) as error:
        parse_csv(sheet(*([valid] * 799), bad), CFG)

    message = str(error.value)
    assert "line 801" in message
    assert "bad-sku" in message
    assert "promoPriceR1" in message


def test_line_number_tracks_embedded_newlines():
    source = io.StringIO(
        HEADER
        + '111,"ref\nwith newline",8.99,7.99,,,,,,,weekly,1\n'
        + "222,,8.99,7.99,,,,,,,weekly,1\n"
    )

    rows = parse_csv(source, CFG)

    assert [row.line for row in rows] == [3, 4]


def test_row_is_hashable_and_frozen():
    rows = parse_csv(sheet(
        "111,,8.99,7.99,2026-08-28T1:00:00-03:00,2026-09-18T1:00:00-03:00,"
        ",,,,weekly,1"
    ), CFG)
    assert len({rows[0], rows[0]}) == 1
    with pytest.raises(FrozenInstanceError):
        rows[0].promo = 1.0
