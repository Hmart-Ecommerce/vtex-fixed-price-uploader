import io
from datetime import datetime, timezone

from vtex_fixed_price_uploader.config import load_config
from vtex_fixed_price_uploader.runner import preflight
from vtex_fixed_price_uploader.verify import comparable, verify

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
CFG = load_config({"accounts": {"R1": "acct_one"}, "never_write": ["m"],
                   "trade_policy": "1"})
HEADER = ("skuId,listPriceR1,promoPriceR1,dateStartR1,dateEndR1,promo_type\n")
GOOD = ("111,8.99,7.99,2026-08-28T1:00:00-03:00,2026-09-18T1:00:00-03:00,w\n")


def entry(value, list_price=None, start=None, end=None, min_qty=1):
    e = {"value": value, "listPrice": list_price, "minQuantity": min_qty,
         "tradePolicyId": "1"}
    if start or end:
        e["dateRange"] = {"from": start, "to": end}
    return e


def test_comparable_ignores_order():
    a = [entry(1.0), entry(2.0)]
    assert comparable(a) == comparable(list(reversed(a)))


def test_comparable_survives_a_none_list_price_tie():
    """Two entries tie on value; one has listPrice None. A naive sort raises."""
    a = [entry(9.99, list_price=None), entry(9.99, list_price=12.99)]
    assert comparable(a) == comparable(list(reversed(a)))


def test_comparable_distinguishes_none_from_zero():
    assert comparable([entry(1.0, list_price=None)]) != comparable(
        [entry(1.0, list_price=0.0)])


def test_comparable_treats_z_and_offset_as_equal():
    a = [entry(1.0, start="2026-08-28T04:00:00Z", end="2026-09-18T04:00:00Z")]
    b = [entry(1.0, start="2026-08-28T04:00:00+00:00",
               end="2026-09-18T04:00:00+00:00")]
    assert comparable(a) == comparable(b)


def test_comparable_detects_a_real_difference():
    assert comparable([entry(1.0)]) != comparable([entry(2.0)])


def test_comparable_ignores_other_trade_policies():
    other = {"value": 5.0, "minQuantity": 1, "tradePolicyId": "2"}
    assert comparable([entry(1.0), other]) == comparable([entry(1.0)])


def make_pre(fetch):
    return preflight(CFG, io.StringIO(HEADER + GOOD), "tok", now=NOW,
                     fetch=fetch, name_fetch=lambda url, timeout=30: [])


def test_verify_matches_when_the_read_back_agrees():
    pre = make_pre(lambda a, s, t, **k: (200, {"basePrice": 8.99,
                                               "fixedPrices": []}))
    expected = pre.compositions[("111", "acct_one")].new_array

    def read_back(account, sku, token, timeout=30, retries=2):
        return 200, {"basePrice": 8.99, "fixedPrices": expected}

    result = verify(CFG, pre, "tok", fetch=read_back, sleep=lambda s: None)
    assert result.matched == 1 and result.mismatched == 0


def test_verify_reports_a_mismatch():
    pre = make_pre(lambda a, s, t, **k: (200, {"basePrice": 8.99,
                                               "fixedPrices": []}))

    def read_back(account, sku, token, timeout=30, retries=2):
        return 200, {"basePrice": 8.99, "fixedPrices": [entry(99.99)]}

    result = verify(CFG, pre, "tok", fetch=read_back, sleep=lambda s: None)
    assert result.mismatched == 1


def test_verify_counts_unreadable_rows():
    pre = make_pre(lambda a, s, t, **k: (200, {"basePrice": 8.99,
                                               "fixedPrices": []}))
    result = verify(CFG, pre, "tok",
                    fetch=lambda a, s, t, **k: (500, None),
                    sleep=lambda s: None)
    assert result.unreadable == 1


def test_verify_flags_rows_still_carrying_several_live_prices():
    pre = make_pre(lambda a, s, t, **k: (200, {"basePrice": 8.99,
                                               "fixedPrices": []}))

    def read_back(account, sku, token, timeout=30, retries=2):
        return 200, {"basePrice": 8.99,
                     "fixedPrices": [entry(1.0), entry(2.0)]}

    result = verify(CFG, pre, "tok", fetch=read_back, sleep=lambda s: None)
    assert result.still_multiple == 1


def test_verify_waits_before_reading():
    pre = make_pre(lambda a, s, t, **k: (200, {"basePrice": 8.99,
                                               "fixedPrices": []}))
    slept = []
    verify(CFG, pre, "tok", wait=120,
           fetch=lambda a, s, t, **k: (200, {"fixedPrices": []}),
           sleep=slept.append)
    assert slept == [120]


def test_verify_treats_an_empty_read_back_as_a_confirmed_failure():
    """404 on read-back after a 200 write is the phantom-200 case itself."""
    pre = make_pre(lambda a, s, t, **k: (200, {"basePrice": 8.99,
                                               "fixedPrices": []}))
    result = verify(CFG, pre, "tok",
                    fetch=lambda a, s, t, **k: (404, None),
                    sleep=lambda s: None)
    assert result.confirmed_empty == 1
    assert result.unreadable == 0
    assert result.matched == 0 and result.mismatched == 0


def test_verify_does_not_count_a_throttled_read_as_a_failure():
    """429 means we could not look, not that the write failed."""
    pre = make_pre(lambda a, s, t, **k: (200, {"basePrice": 8.99,
                                               "fixedPrices": []}))
    result = verify(CFG, pre, "tok",
                    fetch=lambda a, s, t, **k: (429, None),
                    sleep=lambda s: None)
    assert result.unreadable == 1
    assert result.confirmed_empty == 0


def test_verify_treats_the_network_sentinel_as_unreadable():
    pre = make_pre(lambda a, s, t, **k: (200, {"basePrice": 8.99,
                                               "fixedPrices": []}))
    result = verify(CFG, pre, "tok",
                    fetch=lambda a, s, t, **k: (0, None),
                    sleep=lambda s: None)
    assert result.unreadable == 1
    assert result.confirmed_empty == 0


def test_verify_gives_the_two_outcomes_different_verdicts_and_wording():
    pre = make_pre(lambda a, s, t, **k: (200, {"basePrice": 8.99,
                                               "fixedPrices": []}))
    empty = verify(CFG, pre, "tok", fetch=lambda a, s, t, **k: (404, None),
                   sleep=lambda s: None).rows[0]
    blind = verify(CFG, pre, "tok", fetch=lambda a, s, t, **k: (429, None),
                   sleep=lambda s: None).rows[0]

    assert empty["verdict"] == "confirmed_empty"
    assert blind["verdict"] == "unreadable"
    assert empty["detail"] != blind["detail"]
    assert "re-run" in blind["detail"].lower()


def test_verify_keeps_the_raw_status_on_both_kinds_of_row():
    pre = make_pre(lambda a, s, t, **k: (200, {"basePrice": 8.99,
                                               "fixedPrices": []}))
    empty = verify(CFG, pre, "tok", fetch=lambda a, s, t, **k: (404, None),
                   sleep=lambda s: None).rows[0]
    blind = verify(CFG, pre, "tok", fetch=lambda a, s, t, **k: (503, None),
                   sleep=lambda s: None).rows[0]
    assert empty["status"] == 404
    assert blind["status"] == 503
