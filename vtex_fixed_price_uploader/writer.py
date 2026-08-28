"""The one call that changes production, and nothing else.

Kept apart from the runner so the control flow can be read without network
noise, and so a retry policy change touches one function.

A 200 from this endpoint does NOT mean the write took effect - VTEX answers 200
for a trade policy that does not exist. Only the read-back in verify.py is
evidence.
"""

import http.client
import json
import re
import time
import urllib.error
import urllib.request

from vtex_fixed_price_uploader.config import check_account_allowed

PRICING_HOST = "https://api.vtex.com"

# The only trade policy this tool is allowed to write. `load_config` also
# validates it, but that check is three modules away and bypassable - a
# `dataclasses.replace` or a direct `Config(...)` call reaches this function
# with any policy at all. For the file that changes production pricing, the
# invariant is asserted here rather than inherited.
TRADE_POLICY = "1"

# A sku is interpolated into a production write URL and it originates in a
# spreadsheet cell. The account has an exact-match allowlist; this is the sku's
# equivalent. Anything that could open a new path segment, escape upwards, add
# a query or a fragment, or smuggle whitespace is refused before interpolation.
_SKU_PATTERN = re.compile(r"\A[A-Za-z0-9_-]+\Z")

# Seconds to wait between attempts, routed through a module-level name so a
# test can patch it instead of blocking - the same indirection pricing.py uses.
RETRY_BACKOFF_SECONDS = 5
_sleep = time.sleep


def _check_trade_policy(config):
    policy = getattr(config, "trade_policy", None)
    if policy != TRADE_POLICY:
        raise ValueError(
            "this tool only writes trade policy {!r}; refusing a config "
            "carrying trade policy {!r}".format(TRADE_POLICY, policy))


def _check_sku(sku):
    if not isinstance(sku, str) or not _SKU_PATTERN.match(sku):
        raise ValueError(
            "sku must be a plain identifier of letters, digits, '-' or '_'; "
            "refusing {!r}".format(sku))


def _check_payload(payload):
    if not isinstance(payload, list):
        raise ValueError(
            "payload must be a list of price entries; got {}".format(
                type(payload).__name__))
    if not payload:
        raise ValueError(
            "payload must not be empty: this endpoint replaces the whole "
            "policy-1 array, so an empty payload would clear every fixed "
            "price on the sku")
    if not all(isinstance(entry, dict) for entry in payload):
        raise ValueError(
            "payload must contain price entry objects only; refusing a list "
            "holding {}".format(", ".join(sorted({
                type(entry).__name__
                for entry in payload
                if not isinstance(entry, dict)
            }))))


def _scrub(text, token):
    """Keep the token out of anything that reaches operator-facing output.

    The excerpt is unvalidated remote body text. A proxy that answers 400 by
    echoing the offending request header is the realistic way the token ends
    up in a message the operator reads or pastes into a ticket.
    """
    if token and isinstance(text, str):
        return text.replace(token, "[redacted]")
    return text


def post_fixed(config, account, sku, payload, token, timeout=30,
               retries=1) -> tuple[int, str]:
    """Replace the policy-1 array. Returns (http_status, error excerpt).

    `retries` counts retries, not attempts: the default `retries=1` makes TWO
    attempts, `retries=0` makes one, `retries=3` makes four.

    Status 0 means the network failed and the write may or may not have landed -
    a different situation from any HTTP status, and the caller must treat it as
    unknown rather than failed.

    The account guard runs before the URL is built, so a refused account never
    reaches string interpolation and cannot leak into a log line. The trade
    policy, the sku and the payload are validated in the same window, before
    any of them reaches the URL or the wire.
    """
    check_account_allowed(config, account)
    _check_trade_policy(config)
    _check_sku(sku)
    _check_payload(payload)

    url = "{}/{}/pricing/prices/{}/fixed/{}".format(
        PRICING_HOST, account, sku, TRADE_POLICY)
    # Hoisted out of the retry loop on purpose. This endpoint replaces the
    # whole array, so retry idempotency depends on every attempt putting
    # byte-identical bytes on the wire.
    body = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "VtexIdclientAutCookie": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, ""
        except urllib.error.HTTPError as exc:
            detail = _scrub(
                exc.read().decode("utf-8", "replace"), token)[:300] \
                if exc.fp else ""
            if exc.code == 401:
                return 401, "login rejected"
            if exc.code == 403:
                # Authenticated but not permitted on pricing. The operator
                # sees the same symptom as a bad token, so it halts the run
                # the same way instead of retrying and then producing one
                # identical per-row failure for every remaining row. The body
                # is discarded for the same reason 401 discards it.
                return 403, "not permitted on pricing"
            if exc.code < 500 or attempt >= retries:
                return exc.code, detail
        except urllib.error.URLError as exc:
            if attempt >= retries:
                return 0, _scrub(str(exc.reason), token)[:300]
        except (OSError, http.client.HTTPException) as exc:
            # urllib wraps only the request phase into URLError; anything that
            # fails while reading the response - a reset, a read timeout, a
            # RemoteDisconnected, an IncompleteRead - is re-raised bare. Those
            # are the canonical "the server got the POST and we do not know
            # what it did with it" cases, so they must reach the caller as the
            # status-0 sentinel rather than escape as an exception.
            if attempt >= retries:
                return 0, _scrub(str(exc) or type(exc).__name__, token)[:300]
        attempt += 1
        _sleep(RETRY_BACKOFF_SECONDS)
