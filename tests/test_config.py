import json
import pytest
from vtex_fixed_price_uploader.config import (
    Config, DisallowedAccount, check_account_allowed, load_config)

RAW = {
    "accounts": {"R1": "acct_one", "R2": "acct_two"},
    "never_write": ["acct_master"],
    "trade_policy": "1",
}


def test_load_config_from_dict():
    cfg = load_config(RAW)
    assert cfg.accounts == {"R1": "acct_one", "R2": "acct_two"}
    assert cfg.never_write == ("acct_master",)
    assert cfg.trade_policy == "1"


def test_load_config_from_file(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(RAW), encoding="utf-8")
    assert load_config(str(path)).accounts["R1"] == "acct_one"


def test_allowed_account_passes():
    check_account_allowed(load_config(RAW), "acct_one")


def test_unknown_account_is_refused():
    with pytest.raises(DisallowedAccount):
        check_account_allowed(load_config(RAW), "acct_stranger")


def test_never_write_account_is_refused():
    with pytest.raises(DisallowedAccount):
        check_account_allowed(load_config(RAW), "acct_master")


def test_never_write_wins_even_if_also_in_allowlist():
    """Defence in depth: a typo adding the master to accounts must not open it."""
    raw = {"accounts": {"R1": "acct_master"}, "never_write": ["acct_master"],
           "trade_policy": "1"}
    with pytest.raises(DisallowedAccount):
        check_account_allowed(load_config(raw), "acct_master")


def test_missing_accounts_key_is_rejected():
    with pytest.raises(ValueError):
        load_config({"never_write": [], "trade_policy": "1"})


def test_empty_accounts_is_rejected():
    with pytest.raises(ValueError):
        load_config({"accounts": {}, "never_write": [], "trade_policy": "1"})


def test_trade_policy_other_than_one_is_rejected():
    with pytest.raises(ValueError):
        load_config({"accounts": {"R1": "acct_one"}, "never_write": [],
                     "trade_policy": "2"})
