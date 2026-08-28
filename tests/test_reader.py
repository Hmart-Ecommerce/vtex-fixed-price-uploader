import json
import logging
import os

import pytest

from vtex_fixed_price_uploader.config import load_config
from vtex_fixed_price_uploader.pricing import NETWORK_FAILURE_STATUS
from vtex_fixed_price_uploader.reader import (
    MAX_FAILED_READ_FRACTION, AuthenticationError, UnhealthySnapshot,
    is_failed_read, load_snapshot, read_all, save_snapshot, snapshot_hash,
    status_counts)

CFG = load_config({"accounts": {"R1": "acct_one", "R2": "acct_two"},
                   "never_write": ["acct_master"], "trade_policy": "1"})


def fake_fetch(account, sku, token, timeout=30, retries=2):
    if sku == "999":
        return 404, None
    return 200, {"basePrice": 8.99, "fixedPrices": []}


def test_reads_every_account_for_every_sku():
    reads = read_all(CFG, ["111", "222"], "tok", fetch=fake_fetch)
    assert set(reads) == {("111", "acct_one"), ("111", "acct_two"),
                          ("222", "acct_one"), ("222", "acct_two")}


def test_404_is_recorded_not_dropped():
    reads = read_all(CFG, ["999"], "tok", fetch=fake_fetch)
    assert reads[("999", "acct_one")] == (404, None)


def test_401_halts_the_read():
    def unauthorized(account, sku, token, timeout=30, retries=2):
        return 401, None

    with pytest.raises(AuthenticationError):
        read_all(CFG, ["111"], "expired-token", fetch=unauthorized)


def test_the_401_halt_never_reveals_the_token():
    def unauthorized(account, sku, token, timeout=30, retries=2):
        return 401, None

    with pytest.raises(AuthenticationError) as caught:
        read_all(CFG, ["111"], "expired-token", fetch=unauthorized)
    assert "expired-token" not in str(caught.value)
    assert "expired-token" not in repr(caught.value)


def test_progress_is_reported_for_every_pair():
    seen = []
    read_all(CFG, ["111", "222"], "tok", fetch=fake_fetch,
             progress=lambda done, total: seen.append((done, total)))
    assert len(seen) == 4
    assert seen[-1] == (4, 4)


def test_duplicate_skus_are_read_once():
    calls = []

    def counting(account, sku, token, timeout=30, retries=2):
        calls.append((account, sku))
        return 200, {}

    read_all(CFG, ["111", "111", "111"], "tok", fetch=counting)
    assert len(calls) == 2


def test_snapshot_round_trips(tmp_path):
    reads = read_all(CFG, ["111"], "tok", fetch=fake_fetch)
    path = str(tmp_path / "snap.json")
    save_snapshot(reads, path)
    assert load_snapshot(path) == reads


def test_snapshot_hash_is_stable():
    reads = read_all(CFG, ["111"], "tok", fetch=fake_fetch)
    assert snapshot_hash(reads) == snapshot_hash(dict(reversed(list(reads.items()))))


def test_snapshot_hash_changes_with_content():
    a = read_all(CFG, ["111"], "tok", fetch=fake_fetch)
    b = dict(a)
    b[("111", "acct_one")] = (200, {"basePrice": 1.00, "fixedPrices": []})
    assert snapshot_hash(a) != snapshot_hash(b)


def test_a_transport_failure_becomes_the_network_sentinel():
    """A fetch that dies in transport is recorded, payload and all."""
    def failing(account, sku, token, timeout=30, retries=2):
        raise TimeoutError("timed out")

    reads = read_all(CFG, ["111"], "tok", fetch=failing)
    assert reads[("111", "acct_one")] == (NETWORK_FAILURE_STATUS, None)


def test_a_programming_error_is_not_masked_as_a_network_failure():
    def wrong_signature(account, sku, token, timeout=30, retries=2):
        raise TypeError("fetch() got an unexpected keyword argument")

    with pytest.raises(TypeError):
        read_all(CFG, ["111"], "tok", fetch=wrong_signature)


def test_the_halt_exception_is_not_swallowed_by_the_transport_handler():
    def unauthorized(account, sku, token, timeout=30, retries=2):
        raise AuthenticationError("the credential was rejected")

    with pytest.raises(AuthenticationError):
        read_all(CFG, ["111"], "tok", fetch=unauthorized)


def test_transport_failure_text_is_retained_for_the_caller():
    def failing(account, sku, token, timeout=30, retries=2):
        raise TimeoutError("connection to the pricing host timed out")

    errors = {}
    read_all(CFG, ["111"], "sekrit", fetch=failing, errors=errors)
    assert "timed out" in errors[("111", "acct_one")]
    assert "TimeoutError" in errors[("111", "acct_one")]


def test_retained_failure_text_never_carries_the_token():
    def leaking(account, sku, token, timeout=30, retries=2):
        raise TimeoutError("timeout with header sekrit attached")

    errors = {}
    read_all(CFG, ["111"], "sekrit", fetch=leaking, errors=errors)
    assert "sekrit" not in errors[("111", "acct_one")]


def test_a_transport_failure_is_logged_without_the_token(caplog):
    def leaking(account, sku, token, timeout=30, retries=2):
        raise TimeoutError("timeout with header sekrit attached")

    with caplog.at_level(logging.WARNING):
        read_all(CFG, ["111"], "sekrit", fetch=leaking)
    assert "TimeoutError" in caplog.text
    assert "sekrit" not in caplog.text


@pytest.mark.parametrize("status,failed", [
    (200, False),
    (404, False),
    (401, True),
    (429, True),
    (500, True),
    (NETWORK_FAILURE_STATUS, True),
])
def test_is_failed_read_classifies_every_status(status, failed):
    """One rule, exported once, so no consumer has to re-derive it."""
    assert is_failed_read(status) is failed


def test_status_counts_summarises_the_read():
    reads = {("111", "acct_one"): (200, {}),
             ("111", "acct_two"): (404, None),
             ("222", "acct_one"): (429, None)}
    assert status_counts(reads) == {200: 1, 404: 1, 429: 1}


def test_save_snapshot_records_the_health_summary(tmp_path):
    reads = read_all(CFG, ["111"], "tok", fetch=fake_fetch)
    path = str(tmp_path / "snap.json")
    save_snapshot(reads, path)
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    assert raw["health"] == {"200": 2}


def test_save_snapshot_refuses_a_mostly_failed_read(tmp_path):
    reads = {("111", "acct_one"): (429, None),
             ("222", "acct_one"): (NETWORK_FAILURE_STATUS, None),
             ("333", "acct_one"): (200, {})}
    path = str(tmp_path / "snap.json")
    with pytest.raises(UnhealthySnapshot):
        save_snapshot(reads, path)
    assert not os.path.exists(path)


def test_404s_are_ordinary_and_never_block_a_snapshot(tmp_path):
    reads = {("111", "acct_one"): (404, None),
             ("222", "acct_one"): (404, None),
             ("333", "acct_one"): (200, {})}
    path = str(tmp_path / "snap.json")
    save_snapshot(reads, path)
    assert load_snapshot(path) == reads


def test_the_failed_read_threshold_is_a_named_fraction():
    assert 0 < MAX_FAILED_READ_FRACTION < 1


def test_a_failing_progress_callback_never_loses_a_read():
    """A cosmetic widget bug must not destroy an eleven-minute read."""
    def broken(done, total):
        if done == 2:
            raise RuntimeError("the notebook widget exploded")

    reads = read_all(CFG, ["111", "222"], "tok", fetch=fake_fetch,
                     progress=broken)
    assert len(reads) == 4
    assert reads[("222", "acct_two")] == (200, {"basePrice": 8.99,
                                                "fixedPrices": []})


def test_a_progress_callback_that_always_fails_still_completes():
    def always_broken(done, total):
        raise ValueError("no display")

    reads = read_all(CFG, ["111"], "tok", fetch=fake_fetch,
                     progress=always_broken)
    assert len(reads) == 2


def test_integer_skus_are_coerced_at_the_boundary():
    """111 and "111" are one SKU, fetched once, keyed once."""
    calls = []

    def counting(account, sku, token, timeout=30, retries=2):
        calls.append(sku)
        return 200, {}

    reads = read_all(CFG, [111, "111"], "tok", fetch=counting)
    assert set(reads) == {("111", "acct_one"), ("111", "acct_two")}
    assert len(calls) == 2
    assert all(isinstance(sku, str) for sku in calls)


def test_an_integer_sku_snapshot_round_trips(tmp_path):
    reads = read_all(CFG, [111], "tok", fetch=fake_fetch)
    path = str(tmp_path / "snap.json")
    save_snapshot(reads, path)
    assert load_snapshot(path) == reads


def test_integer_and_string_skus_hash_alike():
    assert (snapshot_hash(read_all(CFG, [111], "tok", fetch=fake_fetch))
            == snapshot_hash(read_all(CFG, ["111"], "tok", fetch=fake_fetch)))


def test_a_colliding_snapshot_key_is_refused_not_silently_merged():
    """Two entries must never serialise to one key and a clean hash."""
    reads = {(111, "acct_one"): (200, {"basePrice": 8.99}),
             ("111", "acct_one"): (404, None)}
    with pytest.raises(ValueError):
        snapshot_hash(reads)


def test_an_account_name_containing_the_delimiter_round_trips(tmp_path):
    """`load_config` accepts any non-empty account name, so the key must cope."""
    reads = {("111", "acct|one"): (200, {"basePrice": 8.99}),
             ("111", "acct_two"): (404, None)}
    path = str(tmp_path / "snap.json")
    save_snapshot(reads, path)
    assert load_snapshot(path) == reads


def test_the_delimiter_cannot_forge_a_different_pair():
    a = {("111|x", "acct_one"): (200, {})}
    b = {("111", "x|acct_one"): (200, {})}
    assert snapshot_hash(a) != snapshot_hash(b)


def test_a_backslash_in_a_name_round_trips(tmp_path):
    reads = {("111", "acct\\\\one"): (200, {"basePrice": 8.99})}
    path = str(tmp_path / "snap.json")
    save_snapshot(reads, path)
    assert load_snapshot(path) == reads


def test_read_all_declares_the_planned_return_annotation():
    assert (read_all.__annotations__["return"]
            == dict[tuple[str, str], tuple[int, dict | None]])
