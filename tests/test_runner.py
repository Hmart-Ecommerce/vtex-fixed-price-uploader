import io
from datetime import datetime, timezone

import pytest

from vtex_fixed_price_uploader.auth import TokenExpiringSoon
from vtex_fixed_price_uploader.config import load_config
from vtex_fixed_price_uploader.runner import (
    CredentialRejected,
    apply,
    check_credential,
    preflight,
)
from vtex_fixed_price_uploader.writelog import WriteLog

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
CFG = load_config({"accounts": {"R1": "acct_one", "R2": "acct_two"},
                   "never_write": ["acct_master"], "trade_policy": "1"})

HEADER = ("skuId,listPriceR1,promoPriceR1,dateStartR1,dateEndR1,"
          "listPriceR2,promoPriceR2,dateStartR2,dateEndR2,promo_type\n")
GOOD = ("111,8.99,7.99,2026-08-28T1:00:00-03:00,2026-09-18T1:00:00-03:00,"
        "8.99,7.99,2026-08-28T1:00:00-03:00,2026-09-18T1:00:00-03:00,weekly\n")


def sheet(body=GOOD):
    return io.StringIO(HEADER + body)


def ok_fetch(account, sku, token, timeout=30, retries=2):
    return 200, {"basePrice": 8.99, "fixedPrices": []}


def run_preflight(body=GOOD, fetch=ok_fetch):
    return preflight(CFG, sheet(body), "tok", now=NOW, fetch=fetch,
                     name_fetch=lambda url, timeout=30: [])


def test_preflight_produces_a_composition_per_pair():
    pre = run_preflight()
    assert set(pre.compositions) == {("111", "acct_one"), ("111", "acct_two")}


def test_preflight_write_pairs_exclude_blocked_rows():
    body = ("111,8.99,0,2026-08-28T1:00:00-03:00,2026-09-18T1:00:00-03:00,"
            "8.99,7.99,2026-08-28T1:00:00-03:00,2026-09-18T1:00:00-03:00,w\n")
    pre = run_preflight(body)
    assert ("111", "acct_one") not in pre.write_pairs
    assert ("111", "acct_two") in pre.write_pairs


def test_preflight_hashes_are_populated():
    pre = run_preflight()
    assert len(pre.csv_hash) == 64 and len(pre.snapshot_hash) == 64


def test_preflight_reports_progress():
    seen = []
    preflight(CFG, sheet(), "tok", now=NOW, fetch=ok_fetch,
              progress=lambda d, t: seen.append((d, t)),
              name_fetch=lambda url, timeout=30: [])
    assert seen and seen[-1][0] == seen[-1][1]


def test_preflight_never_writes(monkeypatch):
    import vtex_fixed_price_uploader.writer as writer_module

    def explode(*a, **k):
        raise AssertionError("preflight must never write")

    monkeypatch.setattr(writer_module, "post_fixed", explode)
    run_preflight()


def test_apply_writes_every_pair(tmp_path):
    pre = run_preflight()
    calls = []

    def fake_post(config, account, sku, payload, token, **kw):
        calls.append((sku, account))
        return 200, ""

    log = WriteLog(str(tmp_path / "w.jsonl"))
    result = apply(CFG, pre, "tok", log, post=fake_post, fetch=ok_fetch)
    assert result.written == 2 and result.failed == 0
    assert set(calls) == pre.write_pairs


def test_apply_halts_the_whole_run_on_401(tmp_path):
    pre = run_preflight()
    calls = []

    def fake_post(config, account, sku, payload, token, **kw):
        calls.append(sku)
        return 401, "login rejected"

    log = WriteLog(str(tmp_path / "w.jsonl"))
    result = apply(CFG, pre, "tok", log, post=fake_post, fetch=ok_fetch)
    assert result.halted
    assert len(calls) == 1
    assert result.written == 0


def test_apply_records_a_failure_and_continues(tmp_path):
    pre = run_preflight()
    seen = []

    def fake_post(config, account, sku, payload, token, **kw):
        seen.append(account)
        return (500, "boom") if account == "acct_one" else (200, "")

    log = WriteLog(str(tmp_path / "w.jsonl"))
    result = apply(CFG, pre, "tok", log, post=fake_post, fetch=ok_fetch)
    assert result.written == 1 and result.failed == 1
    assert not result.halted


def test_apply_skips_pairs_already_in_the_log(tmp_path):
    pre = run_preflight()
    path = str(tmp_path / "w.jsonl")
    log = WriteLog(path)
    log.begin(2, pre.csv_hash, pre.snapshot_hash)
    log.append("111", "acct_one", 200, 1)

    calls = []

    def fake_post(config, account, sku, payload, token, **kw):
        calls.append(account)
        return 200, ""

    result = apply(CFG, pre, "tok", log, post=fake_post, fetch=ok_fetch)
    assert calls == ["acct_two"]
    assert result.skipped == 1


def test_apply_finishes_the_log_on_success(tmp_path):
    pre = run_preflight()
    log = WriteLog(str(tmp_path / "w.jsonl"))
    apply(CFG, pre, "tok", log,
          post=lambda *a, **k: (200, ""), fetch=ok_fetch)
    assert log.unfinished() is None


def test_apply_leaves_the_log_open_when_halted(tmp_path):
    pre = run_preflight()
    log = WriteLog(str(tmp_path / "w.jsonl"))
    apply(CFG, pre, "tok", log, post=lambda *a, **k: (401, "no"),
          fetch=ok_fetch)
    assert log.unfinished() is not None


def test_apply_reports_progress(tmp_path):
    pre = run_preflight()
    seen = []
    log = WriteLog(str(tmp_path / "w.jsonl"))
    apply(CFG, pre, "tok", log, post=lambda *a, **k: (200, ""),
          progress=lambda d, t: seen.append((d, t)), fetch=ok_fetch)
    assert seen[-1] == (2, 2)


def test_credential_accepts_a_working_login():
    check_credential(CFG, "tok", fetch=ok_fetch)


def test_credential_rejects_a_401():
    with pytest.raises(CredentialRejected):
        check_credential(CFG, "tok",
                         fetch=lambda a, s, t, **k: (401, None))


def test_credential_tolerates_a_404_probe():
    """A 404 proves the credential works; the probe SKU simply does not exist."""
    check_credential(CFG, "tok", fetch=lambda a, s, t, **k: (404, None))


def test_credential_error_never_contains_the_token():
    try:
        check_credential(CFG, "super-secret", fetch=lambda a, s, t, **k: (401, None))
    except CredentialRejected as exc:
        assert "super-secret" not in str(exc)
    else:
        pytest.fail("expected CredentialRejected")


def test_preflight_refuses_a_credential_that_expires_too_soon():
    import base64
    import json as json_module
    from datetime import timedelta

    def seg(obj):
        return base64.urlsafe_b64encode(
            json_module.dumps(obj).encode()).rstrip(b"=").decode()

    dying = "{}.{}.sig".format(
        seg({"alg": "x"}),
        seg({"exp": int((NOW + timedelta(seconds=0)).timestamp())}))

    with pytest.raises(TokenExpiringSoon):
        preflight(CFG, sheet(), dying, now=NOW, fetch=ok_fetch,
                  name_fetch=lambda url, timeout=30: [])


def test_preflight_credential_check_can_be_skipped():
    pre = preflight(CFG, sheet(), "tok", now=NOW, fetch=ok_fetch,
                    skip_credential_check=True,
                    name_fetch=lambda url, timeout=30: [])
    assert pre.rows


def test_apply_halts_before_writing_when_the_credential_died(tmp_path):
    pre = run_preflight()
    calls = []

    def fake_post(config, account, sku, payload, token, **kw):
        calls.append(sku)
        return 200, ""

    log = WriteLog(str(tmp_path / "w.jsonl"))
    result = apply(CFG, pre, "tok", log, post=fake_post,
                   fetch=lambda a, s, t, **k: (401, None))
    assert result.halted and calls == []


def test_apply_proceeds_when_the_recheck_passes(tmp_path):
    pre = run_preflight()
    log = WriteLog(str(tmp_path / "w.jsonl"))
    result = apply(CFG, pre, "tok", log, post=lambda *a, **k: (200, ""),
                   fetch=ok_fetch)
    assert result.written == 2
