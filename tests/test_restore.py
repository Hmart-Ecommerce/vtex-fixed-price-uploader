from vtex_fixed_price_uploader.config import load_config
from vtex_fixed_price_uploader.restore import pairs_from_log, restore
from vtex_fixed_price_uploader.writelog import WriteLog

CFG = load_config({"accounts": {"R1": "acct_one"}, "never_write": ["acct_master"],
                   "trade_policy": "1"})


def entry(value, end=None):
    e = {"value": value, "listPrice": None, "minQuantity": 1,
         "tradePolicyId": "1"}
    if end:
        e["dateRange"] = {"from": "2026-01-01T00:00:00Z", "to": end}
    return e


SNAPSHOT = {("111", "acct_one"): (200, {"basePrice": 8.99, "fixedPrices": [
    entry(6.99, end="2026-05-01T00:00:00Z"), entry(7.99)]})}


def test_pairs_from_log_lists_written_pairs(tmp_path):
    path = str(tmp_path / "w.jsonl")
    log = WriteLog(path)
    log.begin(2, "c", "s")
    log.append("111", "acct_one", 200, 1)
    log.append("222", "acct_one", 500, 0)
    log.finish()
    assert pairs_from_log(path) == [("111", "acct_one")]


def test_restore_posts_the_exact_prior_array(tmp_path):
    seen = {}

    def fake_post(config, account, sku, payload, token, **kw):
        seen["payload"] = payload
        return 200, ""

    log = WriteLog(str(tmp_path / "r.jsonl"))
    result = restore(CFG, SNAPSHOT, [("111", "acct_one")], "tok", log,
                     post=fake_post)
    assert result.restored == 1
    assert [e["value"] for e in seen["payload"]] == [6.99, 7.99]


def test_restore_keeps_expired_entries(tmp_path):
    """A faithful undo, not a tidy one."""
    seen = {}

    def fake_post(config, account, sku, payload, token, **kw):
        seen["payload"] = payload
        return 200, ""

    log = WriteLog(str(tmp_path / "r.jsonl"))
    restore(CFG, SNAPSHOT, [("111", "acct_one")], "tok", log, post=fake_post)
    assert any("dateRange" in e for e in seen["payload"])


def test_restore_of_an_empty_prior_array_writes_nothing(tmp_path):
    """An empty policy-1 array is not restorable through this endpoint.

    The write replaces the whole array, so posting [] would clear every fixed
    price on the sku - a destructive write, not an undo. `writer._check_payload`
    refuses it outright, so restore must recognise the case and skip the pair
    rather than hand writer a payload it will reject.
    """
    snapshot = {("111", "acct_one"): (200, {"fixedPrices": []})}
    calls = []
    log = WriteLog(str(tmp_path / "r.jsonl"))
    result = restore(CFG, snapshot, [("111", "acct_one")], "tok", log,
                     post=lambda *a, **k: calls.append(1) or (200, ""))
    assert calls == [] and result.restored == 0 and result.failed == 0


def test_restore_skips_a_pair_absent_from_the_snapshot(tmp_path):
    calls = []
    log = WriteLog(str(tmp_path / "r.jsonl"))
    result = restore(CFG, SNAPSHOT, [("999", "acct_one")], "tok", log,
                     post=lambda *a, **k: calls.append(1) or (200, ""))
    assert calls == [] and result.restored == 0


def test_restore_skips_a_pair_whose_snapshot_read_failed(tmp_path):
    """A failed read recorded no prior state, so there is nothing to put back."""
    calls = []
    log = WriteLog(str(tmp_path / "r.jsonl"))
    snapshot = {("111", "acct_one"): (429, None), ("222", "acct_one"): (0, None),
                ("333", "acct_one"): (404, None)}
    result = restore(CFG, snapshot,
                     [("111", "acct_one"), ("222", "acct_one"),
                      ("333", "acct_one")], "tok", log,
                     post=lambda *a, **k: calls.append(1) or (200, ""))
    assert calls == [] and result.restored == 0 and result.failed == 0


def test_restore_halts_on_401(tmp_path):
    log = WriteLog(str(tmp_path / "r.jsonl"))
    result = restore(CFG, SNAPSHOT, [("111", "acct_one")], "tok", log,
                     post=lambda *a, **k: (401, "no"))
    assert result.halted and result.restored == 0


def test_restore_halts_on_403(tmp_path):
    """403 is as terminal as 401: every remaining row would fail identically."""
    calls = []
    log = WriteLog(str(tmp_path / "r.jsonl"))
    result = restore(CFG, SNAPSHOT,
                     [("111", "acct_one"), ("111", "acct_one")], "tok", log,
                     post=lambda *a, **k: calls.append(1) or (403, "nope"))
    assert result.halted and result.restored == 0 and len(calls) == 1


def test_restore_refuses_a_forbidden_account(tmp_path):
    import pytest
    from vtex_fixed_price_uploader.config import DisallowedAccount
    from vtex_fixed_price_uploader import writer as writer_module

    snapshot = {("111", "acct_master"): (200, {"fixedPrices": []})}
    log = WriteLog(str(tmp_path / "r.jsonl"))
    with pytest.raises(DisallowedAccount):
        restore(CFG, snapshot, [("111", "acct_master")], "tok", log,
                post=writer_module.post_fixed)


def test_restore_does_not_leak_the_token_into_the_result(tmp_path):
    log = WriteLog(str(tmp_path / "r.jsonl"))
    result = restore(CFG, SNAPSHOT, [("111", "acct_one")], "tok-secret", log,
                     post=lambda *a, **k: (401, "tok-secret rejected"))
    assert "tok-secret" not in result.halted
