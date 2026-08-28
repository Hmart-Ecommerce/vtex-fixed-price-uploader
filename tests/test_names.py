from vtex_fixed_price_uploader.config import load_config
from vtex_fixed_price_uploader.names import fetch_names

RAW = {"accounts": {"R1": "acct_one"}, "never_write": ["acct_master"],
       "trade_policy": "1", "catalog_host": "https://shop.example.com"}
CFG = load_config(RAW)


def fake_fetch(url, timeout=30):
    if "skuId:111" in url:
        return [{"productName": "Fallback Name",
                 "items": [{"itemId": "111", "nameComplete": "Widget 12oz"}]}]
    if "skuId:222" in url:
        return [{"productName": "Only Product Name", "items": []}]
    return []


def test_prefers_the_sku_level_name():
    assert fetch_names(CFG, ["111"], fetch=fake_fetch) == {"111": "Widget 12oz"}


def test_falls_back_to_the_product_name():
    assert fetch_names(CFG, ["222"], fetch=fake_fetch)["222"] == "Only Product Name"


def test_unknown_sku_is_absent_not_blank():
    assert fetch_names(CFG, ["333"], fetch=fake_fetch) == {}


def test_a_failing_lookup_is_skipped_silently():
    def exploding(url, timeout=30):
        raise RuntimeError("catalog down")

    assert fetch_names(CFG, ["111"], fetch=exploding) == {}


def test_no_catalog_host_means_no_requests():
    calls = []

    def counting(url, timeout=30):
        calls.append(url)
        return []

    cfg = load_config({"accounts": {"R1": "acct_one"}, "never_write": [],
                       "trade_policy": "1"})
    assert fetch_names(cfg, ["111"], fetch=counting) == {}
    assert calls == []


def test_duplicate_skus_are_looked_up_once():
    calls = []

    def counting(url, timeout=30):
        calls.append(url)
        return []

    fetch_names(CFG, ["111", "111", "111"], fetch=counting)
    assert len(calls) == 1
