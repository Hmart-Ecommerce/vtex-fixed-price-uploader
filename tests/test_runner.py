import io
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from vtex_fixed_price_uploader.auth import TokenExpiringSoon
from vtex_fixed_price_uploader.config import DisallowedAccount, load_config
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
    assert len(pre.csv_hash) == 64
    assert len(pre.payload_snapshot_hash) == 64
    assert len(pre.resume_pairs_hash) == 64


def test_resume_identity_survives_payloads_changed_by_the_first_session(tmp_path):
    before = run_preflight()

    def changed_fetch(account, sku, token, timeout=30, retries=2):
        return 200, {"basePrice": 8.99,
                     "fixedPrices": [{"tradePolicyId": "1", "value": 7.99}]}

    after = run_preflight(fetch=changed_fetch)
    assert before.payload_snapshot_hash != after.payload_snapshot_hash
    assert before.resume_pairs_hash == after.resume_pairs_hash

    path = str(tmp_path / "w.jsonl")
    first_session = WriteLog(path)
    first_session.begin(2, before.csv_hash, before.resume_pairs_hash)
    first_session.append("111", "acct_one", 200, 1)
    calls = []

    result = apply(
        CFG, after, "tok", WriteLog(path), fetch=ok_fetch,
        post=lambda config, account, sku, payload, token, **kw:
            calls.append((sku, account)) or (200, ""))

    assert result.skipped == 1
    assert calls == [("111", "acct_two")]


def test_apply_explains_and_explicitly_discards_an_unresumable_log(tmp_path):
    pre = run_preflight()
    path = str(tmp_path / "w.jsonl")
    log = WriteLog(path)
    log.begin(2, pre.csv_hash, pre.resume_pairs_hash)
    log.append("111", "acct_one", 200, 1)
    changed = replace(pre, csv_hash="different-csv")
    calls = []

    blocked = apply(
        CFG, changed, "tok", WriteLog(path), fetch=ok_fetch,
        post=lambda *args, **kwargs: calls.append(1) or (200, ""))

    assert "Resume is not possible" in blocked.halted
    assert "1 row" in blocked.halted
    assert "stay written" in blocked.halted
    assert "abandon_unfinished=True" in blocked.halted
    assert WriteLog(path).unfinished() is not None
    assert calls == []

    abandoned = apply(
        CFG, changed, "tok", WriteLog(path), fetch=ok_fetch,
        post=lambda *args, **kwargs: calls.append(1) or (200, ""),
        abandon_unfinished=True)

    assert "Abandoned" in abandoned.halted
    assert "1 row" in abandoned.halted
    assert "stay written" in abandoned.halted
    assert WriteLog(path).unfinished() is None
    assert calls == []


def test_abandonment_does_not_require_a_still_valid_credential(tmp_path):
    pre = run_preflight()
    path = str(tmp_path / "w.jsonl")
    log = WriteLog(path)
    log.begin(2, pre.csv_hash, pre.resume_pairs_hash)
    log.append("111", "acct_one", 200, 1)

    result = apply(
        CFG, pre, "expired", WriteLog(path), abandon_unfinished=True,
        fetch=lambda account, sku, token, **kwargs: (401, None),
        post=lambda *args, **kwargs: pytest.fail("discard must not write"))

    assert "Abandoned" in result.halted
    assert "1 row" in result.halted
    assert WriteLog(path).unfinished() is None


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


def test_apply_guards_every_account_before_opening_the_log(tmp_path):
    pre = run_preflight()
    composition = pre.compositions[("111", "acct_one")]
    unsafe = replace(
        pre,
        write_pairs=frozenset({("111", "acct_stranger")}),
        compositions={("111", "acct_stranger"): composition},
    )
    path = str(tmp_path / "w.jsonl")
    calls = []

    with pytest.raises(DisallowedAccount, match="acct_stranger"):
        apply(
            CFG, unsafe, "tok", WriteLog(path), fetch=ok_fetch,
            post=lambda *args, **kwargs: calls.append(1) or (200, ""))

    assert calls == []
    assert WriteLog(path).unfinished() is None


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


def test_apply_counts_status_zero_as_unknown_not_failed(tmp_path):
    pre = run_preflight()

    def uncertain_post(config, account, sku, payload, token, **kwargs):
        return (0, "connection ended") if account == "acct_one" else (200, "")

    result = apply(
        CFG, pre, "tok", WriteLog(str(tmp_path / "w.jsonl")),
        post=uncertain_post, fetch=ok_fetch)

    assert result.written == 1
    assert result.unknown == 1
    assert result.failed == 0


def test_apply_skips_pairs_already_in_the_log(tmp_path):
    pre = run_preflight()
    path = str(tmp_path / "w.jsonl")
    log = WriteLog(path)
    log.begin(2, pre.csv_hash, pre.resume_pairs_hash)
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


def test_credential_rejects_a_403_with_pricing_permission_guidance():
    with pytest.raises(CredentialRejected, match="cannot read pricing") as caught:
        check_credential(
            CFG, "super-secret",
            fetch=lambda account, sku, token, **kwargs: (403, None))
    assert "re-running will not help" in str(caught.value)
    assert "super-secret" not in str(caught.value)


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


def test_apply_rechecks_headroom_fresh_for_only_remaining_writes(
        tmp_path, monkeypatch):
    import vtex_fixed_price_uploader.runner as runner_module

    pre = run_preflight()
    path = str(tmp_path / "w.jsonl")
    log = WriteLog(path)
    log.begin(2, pre.csv_hash, pre.resume_pairs_hash)
    log.append("111", "acct_one", 200, 1)
    seen = []

    monkeypatch.setattr(
        runner_module, "check_headroom",
        lambda token, read_pairs, write_rows, now:
            seen.append((token, read_pairs, write_rows, now)) or float("inf"))

    apply(CFG, pre, "tok", WriteLog(path), fetch=ok_fetch,
          post=lambda *args, **kwargs: (200, ""))

    apply_calls = [call for call in seen if call[1] == 0]
    assert len(apply_calls) == 1
    assert apply_calls[0][:3] == ("tok", 0, 1)
    assert apply_calls[0][3] > pre.now
