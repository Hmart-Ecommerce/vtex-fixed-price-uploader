"""Read what VTEX actually holds, before deciding anything.

Every configured account is read for every SKU in the sheet - not only the
pairs the sheet names. Rule I1 reports a region the sheet left blank where the
product does exist, and that comparison is impossible without full coverage.

What a snapshot status means, once and for all - `is_failed_read` is the
exported form of this table, and every consumer should use it rather than
re-deriving the rule:

  200  read succeeded; the payload is present.
  404  no price row for that SKU in that account. ORDINARY - roughly one pair
       in ten - and a successful read of "there is nothing here".
  401  the credential was rejected. Never stored: it raises
       `AuthenticationError` and halts the read.
  429  still throttled after the retries in `pricing.fetch_prices`. FAILED:
       nothing was read, and the row's true state is unknown.
  0    `pricing.NETWORK_FAILURE_STATUS` - transport or response-body failure
       with no HTTP status. FAILED, same as above.

Anything other than 200 and 404 is a failed read: the pair's state in VTEX is
UNKNOWN, which is not the same as "unchanged" and must never be treated as a
clean baseline.
"""

import json
import logging
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from vtex_fixed_price_uploader.pricing import (
    NETWORK_FAILURE_STATUS, fetch_prices)
from vtex_fixed_price_uploader.writelog import sha256_of

UNAUTHORIZED_STATUS = 401

_log = logging.getLogger(__name__)

# What a fetch may legitimately raise. `fetch_prices` handles these itself and
# returns the sentinel, but an injected fetch may not, and a transport failure
# is a read outcome, not a bug. Everything else - TypeError from a changed
# signature, AuthenticationError from fix 1 - propagates and stops the read.
TRANSPORT_FAILURES = (
    urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError)

# A read that lost more than this fraction of its pairs is not a baseline.
# 404s are excluded - they are successful reads. The bar is deliberately low:
# a snapshot is the record the writer and `writelog.resume` trust, and a
# quarter of the matrix missing makes every comparison drawn from it a guess.
MAX_FAILED_READ_FRACTION = 0.25


class UnhealthySnapshot(Exception):
    """Raised instead of writing a snapshot too damaged to be a baseline."""


def is_failed_read(status: int) -> bool:
    """True when the pair's state in VTEX is unknown.

    200 and 404 are both successful reads. Everything else - 401, 429, the
    network sentinel, an unexpected 5xx - means nothing was learned.
    """
    return status not in (200, 404)


def status_counts(reads) -> dict[int, int]:
    """Counts per HTTP status across a read matrix - the health summary."""
    counts: dict[int, int] = {}
    for status, _ in reads.values():
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def failed_read_fraction(reads) -> float:
    if not reads:
        return 0.0
    failed = sum(1 for status, _ in reads.values() if is_failed_read(status))
    return failed / len(reads)


class AuthenticationError(Exception):
    """The pricing API rejected the credential; the read must stop.

    A 401 is never recorded as a read. An expired token returns 401 for every
    pair, and a snapshot of nothing but 401s hashes as cleanly as a healthy
    one - `writelog.resume` would then accept a baseline where nothing was
    read. The message never carries the token.
    """


def read_all(config, skus, token, workers=10, progress=None, fetch=None,
             errors=None) -> dict[tuple[str, str], tuple[int, dict | None]]:
    """{(sku, account): (http_status, payload)} for the full matrix.

    `fetch` is injectable so tests never touch the network. `progress(done,
    total)` fires after each completion - eleven silent minutes reads as a hang
    and invites a second click.

    `errors` is an optional mutable mapping the caller supplies to receive
    `{(sku, account): "ExcType: text"}` for every pair that failed in
    transport. The same text is logged at WARNING. Both are stripped of the
    token before they leave this function.
    """
    fetch = fetch or fetch_prices
    accounts = sorted(set(config.accounts.values()))
    # Coerce here, at the boundary. A sheet that yields 111 where another
    # yields "111" would otherwise fetch the same SKU twice and write two
    # entries that collapse into one snapshot key.
    pairs = [(sku, account) for sku in sorted({str(s) for s in skus})
             for account in accounts]

    out, lock, done = {}, threading.Lock(), [0]
    total = len(pairs)

    def work(pair):
        sku, account = pair
        note = None
        try:
            status, data = fetch(account, sku, token)
        except TRANSPORT_FAILURES as exc:
            status, data = NETWORK_FAILURE_STATUS, None
            note = _redacted(
                "{}: {}".format(type(exc).__name__, exc), token)
            _log.warning("read failed for SKU %s in account %s: %s",
                         sku, account, note)
        if status == UNAUTHORIZED_STATUS:
            raise AuthenticationError(
                "the pricing API rejected the credential (HTTP 401) reading "
                "SKU {} in account {}; the read is halted".format(
                    sku, account))
        with lock:
            out[pair] = (status, data)
            if note is not None and errors is not None:
                errors[pair] = note
            done[0] += 1
            if progress:
                try:
                    progress(done[0], total)
                except Exception as exc:
                    # A progress bar is decoration. It may never cost the
                    # caller a read that has already been paid for in
                    # network time.
                    _log.warning(
                        "progress callback raised %s; continuing the read",
                        type(exc).__name__)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, pairs))
    return out


def _redacted(text, token):
    """The token never reaches a log, a message, or a caller's error map."""
    if token:
        text = text.replace(str(token), "***")
    return text


SNAPSHOT_KEY_DELIMITER = "|"


def _escape_key_part(part) -> str:
    """Make a key component unable to forge the delimiter.

    `load_config` accepts any non-empty account name and a sheet can yield any
    SKU text, so neither component can be trusted to be delimiter-free.
    Escaping here keeps the guarantee inside this module rather than depending
    on a rule enforced in config.py, which does not see SKUs at all.
    """
    return (str(part).replace("\\", "\\\\")
            .replace(SNAPSHOT_KEY_DELIMITER, "\\" + SNAPSHOT_KEY_DELIMITER))


def _split_key(key: str) -> tuple[str, str]:
    """Split on the first UNESCAPED delimiter and unescape both halves."""
    parts, current, escaped = [], [], False
    for char in key:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == SNAPSHOT_KEY_DELIMITER and len(parts) == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    if len(parts) != 2:
        raise ValueError("malformed snapshot key {!r}".format(key))
    return parts[0], parts[1]


def _serialisable(reads):
    out = {}
    for (sku, account), (status, data) in reads.items():
        key = "{}{}{}".format(_escape_key_part(sku), SNAPSHOT_KEY_DELIMITER,
                              _escape_key_part(account))
        if key in out:
            raise ValueError(
                "two snapshot entries collapse to the key {!r}; SKUs must be "
                "strings before they reach a snapshot".format(key))
        out[key] = [status, data]
    return out


def snapshot_hash(reads):
    return sha256_of(_serialisable(reads))


def save_snapshot(reads, path) -> dict[int, int]:
    """Write the snapshot and return its health summary.

    Refuses outright when more than `MAX_FAILED_READ_FRACTION` of the pairs
    are failed reads. A snapshot hashes just as cleanly when nothing was read
    as when everything was, so the refusal has to happen here - by the time
    the digest exists it looks healthy.
    """
    health = status_counts(reads)
    fraction = failed_read_fraction(reads)
    if fraction > MAX_FAILED_READ_FRACTION:
        raise UnhealthySnapshot(
            "refusing to write a snapshot: {:.0%} of {} pairs are failed "
            "reads (limit {:.0%}); statuses {}".format(
                fraction, len(reads), MAX_FAILED_READ_FRACTION, health))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"reads": _serialisable(reads), "health": health}, fh)
    return health


def load_snapshot(path):
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, dict) and "reads" in raw:
        raw = raw["reads"]
    out = {}
    for key, (status, data) in raw.items():
        out[_split_key(key)] = (status, data)
    return out
