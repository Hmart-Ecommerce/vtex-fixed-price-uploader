import dataclasses
from datetime import datetime, timezone

import pytest

from vtex_fixed_price_uploader.compose import (
    Composition, compose, expired, overlaps, row_to_entry)
from vtex_fixed_price_uploader.parser import Row

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def dt(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def row(promo=7.99, list_price=8.99, start="2026-08-28T00:00:00",
        end="2026-09-18T00:00:00"):
    return Row(sku="111", code="R1", account="acct_one", promo=promo,
               list_price=list_price,
               start=dt(start) if start else None,
               end=dt(end) if end else None,
               promo_type="weekly", line=2)


def entry(value, start=None, end=None, min_qty=1, policy="1"):
    e = {"value": value, "listPrice": None, "minQuantity": min_qty,
         "tradePolicyId": policy}
    if start or end:
        e["dateRange"] = {"from": start, "to": end}
    return e


def test_expired_entry():
    assert expired(entry(1.0, end="2026-08-01T00:00:00Z"), NOW) is True


def test_open_ended_entry_is_never_expired():
    assert expired(entry(1.0), NOW) is False


def test_future_entry_is_not_expired():
    assert expired(entry(1.0, end="2026-12-01T00:00:00Z"), NOW) is False


def test_overlaps_when_windows_intersect():
    e = entry(1.0, start="2026-08-14T00:00:00Z", end="2026-09-03T00:00:00Z")
    assert overlaps(e, dt("2026-08-28T00:00:00"), dt("2026-09-18T00:00:00")) is True


def test_does_not_overlap_when_entry_ends_before_window():
    e = entry(1.0, start="2026-07-01T00:00:00Z", end="2026-08-01T00:00:00Z")
    assert overlaps(e, dt("2026-08-28T00:00:00"), dt("2026-09-18T00:00:00")) is False


def test_does_not_overlap_when_entry_starts_after_window():
    e = entry(1.0, start="2026-10-01T00:00:00Z", end="2026-10-31T00:00:00Z")
    assert overlaps(e, dt("2026-08-28T00:00:00"), dt("2026-09-18T00:00:00")) is False


def test_open_ended_entry_overlaps_a_bounded_window():
    e = entry(1.0, start="2026-01-01T00:00:00Z")
    assert overlaps(e, dt("2026-08-28T00:00:00"), dt("2026-09-18T00:00:00")) is True


def test_fully_unbounded_entry_overlaps_everything():
    assert overlaps(entry(1.0), dt("2026-08-28T00:00:00"),
                    dt("2026-09-18T00:00:00")) is True


def test_row_to_entry_emits_both_bounds():
    got = row_to_entry(row())
    assert got["value"] == 7.99
    assert got["listPrice"] == 8.99
    assert got["minQuantity"] == 1
    assert got["dateRange"]["from"].startswith("2026-08-28T00:00:00")
    assert got["dateRange"]["to"].startswith("2026-09-18T00:00:00")


def test_row_to_entry_omits_daterange_when_a_bound_is_missing():
    """VTEX requires both bounds when dateRange is present, so a half-open
    window must be expressed as no window at all rather than a partial one."""
    assert "dateRange" not in row_to_entry(row(end=None))
    assert "dateRange" not in row_to_entry(row(start=None))


def test_future_non_overlapping_campaign_survives():
    data = {"fixedPrices": [
        entry(5.99, start="2026-10-01T00:00:00Z", end="2026-10-31T00:00:00Z")]}
    result = compose([row()], data, NOW)
    assert len(result.new_array) == 2
    assert result.kept and result.kept[0]["value"] == 5.99
    assert result.dropped == ()


def test_expired_entry_is_removed():
    data = {"fixedPrices": [
        entry(6.99, start="2026-04-16T00:00:00Z", end="2026-05-01T00:00:00Z")]}
    result = compose([row()], data, NOW)
    assert [e["value"] for e in result.dropped] == [6.99]
    assert len(result.new_array) == 1


def test_overlapping_live_campaign_is_removed():
    data = {"fixedPrices": [
        entry(9.99, start="2026-08-14T00:00:00Z", end="2026-09-03T00:00:00Z")]}
    result = compose([row()], data, NOW)
    assert [e["value"] for e in result.dropped] == [9.99]
    assert len(result.new_array) == 1
    assert result.new_array[0]["value"] == 7.99


def test_open_ended_entry_is_removed():
    data = {"fixedPrices": [entry(4.99)]}
    result = compose([row()], data, NOW)
    assert [e["value"] for e in result.dropped] == [4.99]


def test_wholesale_tier_is_always_preserved():
    """Even when its window overlaps and even when it is expired: a case price
    is a different product dimension and this tool has no mandate over it."""
    data = {"fixedPrices": [
        entry(3.00, min_qty=6, start="2026-08-14T00:00:00Z",
              end="2026-09-03T00:00:00Z"),
        entry(2.00, min_qty=12, end="2026-01-01T00:00:00Z")]}
    result = compose([row()], data, NOW)
    assert sorted(e["minQuantity"] for e in result.kept) == [6, 12]
    assert result.dropped == ()


def test_string_one_min_quantity_is_single_unit_and_removed():
    data = {"fixedPrices": [
        entry(4.99, min_qty="1", start="2026-08-14T00:00:00Z",
              end="2026-12-31T00:00:00Z")]}
    result = compose([row()], data, NOW)
    assert [e["value"] for e in result.dropped] == [4.99]


def test_other_trade_policies_are_invisible():
    data = {"fixedPrices": [entry(1.11, policy="2")]}
    result = compose([row()], data, NOW)
    assert result.kept == ()
    assert result.dropped == ()
    assert len(result.new_array) == 1


def test_two_csv_rows_for_the_same_pair_both_land():
    rows = [row(start="2026-08-28T00:00:00", end="2026-09-05T00:00:00"),
            row(promo=6.99, start="2026-09-06T00:00:00",
                end="2026-09-18T00:00:00")]
    result = compose(rows, {"fixedPrices": []}, NOW)
    assert sorted(e["value"] for e in result.new_array) == [6.99, 7.99]


def test_csv_entries_come_first_in_the_array():
    data = {"fixedPrices": [
        entry(5.99, start="2026-10-01T00:00:00Z", end="2026-10-31T00:00:00Z")]}
    result = compose([row()], data, NOW)
    assert result.new_array[0]["value"] == 7.99


def test_empty_existing_payload_is_fine():
    result = compose([row()], None, NOW)
    assert len(result.new_array) == 1
    assert result.kept == () and result.dropped == ()


def test_composition_is_deterministic():
    data = {"fixedPrices": [
        entry(5.99, start="2026-10-01T00:00:00Z", end="2026-10-31T00:00:00Z"),
        entry(4.99)]}
    first = compose([row()], data, NOW)
    second = compose([row()], data, NOW)
    assert first.new_array == second.new_array


def test_entry_starting_exactly_when_the_csv_window_ends_is_kept():
    """Boundary pin for `starts < end`. The CSV row runs to 2026-09-18 and this
    entry begins at that same instant, so the two do not compete for any day."""
    data = {"fixedPrices": [
        entry(5.99, start="2026-09-18T00:00:00Z", end="2026-10-31T00:00:00Z")]}
    result = compose([row()], data, NOW)
    assert [e["value"] for e in result.kept] == [5.99]
    assert not result.dropped


def test_entry_ending_exactly_when_the_csv_window_starts_is_kept():
    """Boundary pin for `ends > start`. The entry closes at 2026-08-28, the
    same instant the CSV row opens, so it is not an overlap."""
    data = {"fixedPrices": [
        entry(5.49, start="2026-08-20T00:00:00Z", end="2026-08-28T00:00:00Z")]}
    result = compose([row()], data, NOW)
    assert [e["value"] for e in result.kept] == [5.49]
    assert not result.dropped


def test_entry_ending_exactly_at_now_is_expired():
    """Boundary pin for `ends <= now`. An entry whose end bound is exactly the
    run's `now` is over, not live."""
    e = entry(6.49, start="2026-08-01T00:00:00Z", end="2026-08-26T12:00:00Z")
    assert expired(e, NOW) is True
    result = compose([row()], {"fixedPrices": [e]}, NOW)
    assert [x["value"] for x in result.dropped] == [6.49]


def test_min_quantity_of_exactly_two_is_a_wholesale_tier():
    """Boundary pin for the spec's `minQuantity >= 2` threshold. Two units is
    already a case price, so it is preserved even though its window overlaps."""
    data = {"fixedPrices": [
        entry(3.50, min_qty=2, start="2026-08-14T00:00:00Z",
              end="2026-09-03T00:00:00Z")]}
    result = compose([row()], data, NOW)
    assert [e["minQuantity"] for e in result.kept] == [2]
    assert not result.dropped


def test_compose_rejects_a_naive_now():
    """Same guard and same wording as `pricing.is_live`. Without it a naive
    `now` dies mid-run on an opaque datetime comparison TypeError that names
    neither the argument nor the caller at fault."""
    naive = datetime(2026, 8, 26, 12, 0)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        compose([row()], {"fixedPrices": [entry(4.99)]}, naive)


def test_compose_rejects_a_naive_now_even_with_nothing_to_compare():
    """The guard is on the argument, not on whether the run happens to reach a
    comparison, so an empty payload fails just as loudly."""
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        compose([row()], None, datetime(2026, 8, 26, 12, 0))


def test_expired_rejects_a_naive_now():
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        expired(entry(1.0, end="2026-12-01T00:00:00Z"),
                datetime(2026, 8, 26, 12, 0))


def test_the_three_sequences_are_tuples():
    """`Composition` is frozen, so its contents must be too. In the one module
    where an accidental mutation writes wrong prices, a list that happily
    accepts `.append()` is a false promise."""
    result = compose([row()], {"fixedPrices": [entry(4.99)]}, NOW)
    assert isinstance(result.new_array, tuple)
    assert isinstance(result.kept, tuple)
    assert isinstance(result.dropped, tuple)


def test_mutating_the_caller_payload_after_composing_does_not_change_the_result():
    data = {"fixedPrices": [
        entry(5.99, start="2026-10-01T00:00:00Z", end="2026-10-31T00:00:00Z")]}
    result = compose([row()], data, NOW)
    data["fixedPrices"][0]["value"] = 99.99
    data["fixedPrices"][0]["dateRange"]["from"] = "1999-01-01T00:00:00Z"
    assert result.kept[0]["value"] == 5.99
    assert result.kept[0]["dateRange"]["from"] == "2026-10-01T00:00:00Z"
    assert result.new_array[1]["value"] == 5.99


def test_mutating_the_result_does_not_change_the_caller_payload():
    data = {"fixedPrices": [entry(4.99)]}
    result = compose([row()], data, NOW)
    result.dropped[0]["value"] = 99.99
    assert data["fixedPrices"][0]["value"] == 4.99


def test_result_entries_are_not_the_caller_payload_entries():
    data = {"fixedPrices": [entry(4.99)]}
    result = compose([row()], data, NOW)
    assert result.dropped[0] is not data["fixedPrices"][0]
    assert result.dropped[0] == data["fixedPrices"][0]


def test_entry_with_no_trade_policy_is_unrecognised():
    """`policy1` matches on `str(tradePolicyId) == "1"`, so an entry without an
    id lands in neither kept nor dropped and would be deleted by the write with
    no warning. Silent price deletion is the one thing this tool exists to
    prevent, so it is surfaced instead."""
    orphan = {"value": 4.99, "listPrice": None, "minQuantity": 1}
    result = compose([row()], {"fixedPrices": [orphan]}, NOW)
    assert [e["value"] for e in result.unrecognised] == [4.99]
    assert result.kept == () and result.dropped == ()
    assert [e["value"] for e in result.new_array] == [7.99]


def test_float_and_zero_padded_policy_ids_are_unrecognised():
    """`1.0` and `"01"` both mean policy 1 to a human and neither matches the
    string test, so they are refused rather than quietly dropped."""
    data = {"fixedPrices": [entry(4.99, policy=1.0), entry(3.99, policy="01")]}
    result = compose([row()], data, NOW)
    assert sorted(e["value"] for e in result.unrecognised) == [3.99, 4.99]


def test_unparseable_policy_id_is_unrecognised():
    data = {"fixedPrices": [entry(4.99, policy="abc"), entry(3.99, policy=None),
                            entry(2.99, policy=1.5)]}
    result = compose([row()], data, NOW)
    assert sorted(e["value"] for e in result.unrecognised) == [2.99, 3.99, 4.99]


def test_a_genuine_other_policy_is_not_unrecognised():
    """An id that parses to an integer other than 1 is another policy's
    business. It is left alone, exactly as before."""
    data = {"fixedPrices": [entry(1.11, policy="2"), entry(2.22, policy=3),
                            entry(3.33, policy="04")]}
    result = compose([row()], data, NOW)
    assert result.unrecognised == ()
    assert result.kept == () and result.dropped == ()


def test_policy_one_entries_are_never_unrecognised():
    data = {"fixedPrices": [entry(4.99, policy="1"), entry(3.99, policy=1)]}
    result = compose([row()], data, NOW)
    assert result.unrecognised == ()
    assert sorted(e["value"] for e in result.dropped) == [3.99, 4.99]


def test_unrecognised_entries_are_tuples_of_copies():
    orphan = {"value": 4.99, "minQuantity": 1}
    data = {"fixedPrices": [orphan]}
    result = compose([row()], data, NOW)
    assert isinstance(result.unrecognised, tuple)
    assert result.unrecognised[0] is not orphan
    result.unrecognised[0]["value"] = 99.99
    assert orphan["value"] == 4.99


def test_a_row_with_both_dates_blank_deliberately_wipes_the_policy_one_array():
    """This is the spec rule, not an accident. A row with no window becomes an
    entry with no window, and `overlaps` treats a missing CSV bound as open on
    that side - so EVERY single-unit policy-1 entry overlaps it, including a
    campaign months away that no operator would expect this row to touch. W6
    reports it because the entries land in `dropped`; the wholesale tier is
    still exempt. If this test ever fails, the blast radius of a blank-dated
    row has changed and the operator warning must change with it."""
    data = {"fixedPrices": [
        entry(5.99, start="2026-10-01T00:00:00Z", end="2026-10-31T00:00:00Z"),
        entry(9.99, start="2026-08-14T00:00:00Z", end="2026-09-03T00:00:00Z"),
        entry(4.99),
        entry(3.00, min_qty=6, start="2026-10-01T00:00:00Z",
              end="2026-10-31T00:00:00Z")]}
    result = compose([row(start=None, end=None)], data, NOW)
    assert sorted(e["value"] for e in result.dropped) == [4.99, 5.99, 9.99]
    assert [e["value"] for e in result.kept] == [3.00]
    assert [e["value"] for e in result.new_array] == [7.99, 3.00]
    assert "dateRange" not in result.new_array[0]


def test_compose_returns_a_frozen_composition():
    """Puts the `Composition` import to work: the return type is part of the
    contract, and so is the fact that a caller cannot rebind its fields."""
    result = compose([row()], {"fixedPrices": [entry(4.99)]}, NOW)
    assert isinstance(result, Composition)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.new_array = ()
