"""Product names for the report.

The Pricing API returns no name, and a report that says "SKU 7325" instead of
"Tokyo Banana 1.06LB" is unreadable for the person this tool exists for. The
catalog search endpoint is public and needs no credential.

Every failure here is silent by design: a missing name degrades the report, it
does not endanger a price. The caller falls back to the SKU.
"""

import json
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SEARCH_PATH = "/api/catalog_system/pub/products/search"


def _http_get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_name(products, sku) -> str:
    """The SKU-level name, else the product name, else "".

    Every access here can raise on a shape the catalog was not supposed to
    return; the caller runs the whole thing inside its guard.
    """
    if not products:
        return ""
    product = products[0]
    name = ""
    for item in product.get("items") or []:
        if str(item.get("itemId")) == str(sku):
            name = item.get("nameComplete") or item.get("name") or ""
    return name or product.get("productName") or ""


def fetch_names(config, skus, workers=8, fetch=None) -> dict[str, str]:
    """{sku: product name}. Absent when unknown - never blank, never the SKU."""
    if not config.catalog_host:
        return {}
    fetch = fetch or _http_get_json

    out, lock = {}, threading.Lock()

    def work(sku):
        try:
            url = "{}{}?{}".format(
                config.catalog_host.rstrip("/"), SEARCH_PATH,
                urllib.parse.urlencode(
                    {"fq": "skuId:{}".format(sku)}, safe=":"))
            name = _extract_name(fetch(url), sku)
        except Exception:
            # Deliberately total. A gateway that answers an incident with a
            # JSON error object instead of an array, a bare string body, a
            # malformed items entry - none of it may take down a price
            # preflight over a cosmetic label. No name is the right answer to
            # every one of them.
            return
        if name:
            with lock:
                out[str(sku)] = name

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, sorted(set(str(s) for s in skus))))
    return out
