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


def test_restore_of_a_prior_empty_array_posts_an_empty_array(tmp_path):
    """"The sku had no fixed price" is a prior state, and the commonest one.

    A successful read showing an empty policy-1 array is knowledge, not
    absence of knowledge. The only way to express it through this endpoint is
    to post [], so restore opts in to the writer's empty-payload waiver here.
    Skipping instead - which is what shipped - made rollback incapable of
    undoing the first price ever put on a sku: 18 of 18 pairs were left live.
    """
    snapshot = {("111", "acct_one"): (200, {"basePrice": 8.99,
                                            "fixedPrices": []})}
    seen = []
    log = WriteLog(str(tmp_path / "r.jsonl"))

    def fake_post(config, account, sku, payload, token, **kw):
        seen.append((payload, kw))
        return 200, ""

    result = restore(CFG, snapshot, [("111", "acct_one")], "tok", log,
                     post=fake_post)
    assert [payload for payload, _kw in seen] == [[]]
    assert seen[0][1].get("allow_empty") is True
    assert result.restored == 1
    assert result.failed == 0 and result.skipped == 0


def test_restore_of_a_prior_empty_array_survives_the_real_writer(tmp_path,
                                                                 monkeypatch):
    """The waiver restore passes must be the one the real writer accepts.

    The injected fakes above would stay green against a writer that still
    refused []. This closes that gap by driving the actual `post_fixed`.
    """
    from vtex_fixed_price_uploader import writer as writer_module

    class FakeResponse:
        status = 200
        def read(self):
            return b""
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = req.data.decode()
        return FakeResponse()

    monkeypatch.setattr(writer_module.urllib.request, "urlopen", fake_urlopen)
    snapshot = {("111", "acct_one"): (200, {"fixedPrices": []})}
    log = WriteLog(str(tmp_path / "r.jsonl"))
    result = restore(CFG, snapshot, [("111", "acct_one")], "tok", log,
                     post=writer_module.post_fixed)
    assert seen["body"] == "[]"
    assert result.restored == 1


def test_restore_skips_a_pair_the_snapshot_recorded_as_404(tmp_path):
    """404 is a successful read, but not one that says the array was empty.

    A 404 says the pricing record was not found, which is not the same claim
    as "policy 1 held no entries". Posting [] on it would be a guess, and a
    guess here clears the sku.
    """
    calls = []
    log = WriteLog(str(tmp_path / "r.jsonl"))
    snapshot = {("111", "acct_one"): (404, None),
                ("222", "acct_one"): (404, {"fixedPrices": []})}
    result = restore(CFG, snapshot,
                     [("111", "acct_one"), ("222", "acct_one")], "tok", log,
                     post=lambda *a, **k: calls.append(1) or (200, ""))
    assert calls == []
    assert result.restored == 0 and result.failed == 0 and result.skipped == 2


def test_restore_skips_a_pair_absent_from_the_snapshot(tmp_path):
    calls = []
    log = WriteLog(str(tmp_path / "r.jsonl"))
    result = restore(CFG, SNAPSHOT, [("999", "acct_one")], "tok", log,
                     post=lambda *a, **k: calls.append(1) or (200, ""))
    assert calls == [] and result.restored == 0 and result.skipped == 1


def test_restore_skips_a_pair_whose_snapshot_read_failed(tmp_path):
    """A failed read recorded no prior state, so there is nothing to put back."""
    calls = []
    log = WriteLog(str(tmp_path / "r.jsonl"))
    snapshot = {("111", "acct_one"): (429, None), ("222", "acct_one"): (0, None),
                ("333", "acct_one"): (404, None),
                ("444", "acct_one"): (500, {"fixedPrices": []})}
    result = restore(CFG, snapshot,
                     [("111", "acct_one"), ("222", "acct_one"),
                      ("333", "acct_one"), ("444", "acct_one")], "tok", log,
                     post=lambda *a, **k: calls.append(1) or (200, ""))
    assert calls == [] and result.restored == 0 and result.failed == 0
    assert result.skipped == 4


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
