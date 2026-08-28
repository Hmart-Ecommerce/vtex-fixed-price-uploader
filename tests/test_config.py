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


@pytest.mark.parametrize("never_write", [
    "acct_master",
    {"acct_master": True},
    ["acct_master", 1],
    1,
])
def test_never_write_must_be_a_list_or_tuple_of_strings(never_write):
    raw = {**RAW, "never_write": never_write}
    with pytest.raises(ValueError):
        load_config(raw)


@pytest.mark.parametrize("raw", [
    {"accounts": {"R1": "acct_one"}, "never_write": None},
    {"accounts": {"R1": "acct_one"}},
])
def test_missing_or_null_never_write_means_an_empty_tuple(raw):
    assert load_config(raw).never_write == ()


@pytest.mark.parametrize("source", [None, []])
def test_non_mapping_config_source_is_rejected_cleanly(source):
    with pytest.raises(ValueError, match="configuration source must be a mapping"):
        load_config(source)


def test_config_file_with_non_mapping_json_is_rejected_cleanly(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="configuration source must be a mapping"):
        load_config(str(path))


@pytest.mark.parametrize("accounts", [
    {1: "acct_one"},
    {"R1": {"nested": "x"}},
    {"R1": ""},
])
def test_accounts_must_map_string_keys_to_non_empty_string_values(accounts):
    raw = {**RAW, "accounts": accounts}
    with pytest.raises(ValueError, match="accounts"):
        load_config(raw)


def test_config_field_annotations_are_precise():
    assert Config.__annotations__["accounts"] == dict[str, str]
    assert Config.__annotations__["never_write"] == tuple[str, ...]


def test_catalog_host_is_optional():
    assert load_config(RAW).catalog_host is None


def test_catalog_host_is_read_when_present():
    raw = dict(RAW, catalog_host="https://shop.example.com")
    assert load_config(raw).catalog_host == "https://shop.example.com"
