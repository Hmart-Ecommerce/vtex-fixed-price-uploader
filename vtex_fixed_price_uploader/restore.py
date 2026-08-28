"""Put back exactly what was there before.

Faithful, not tidy: expired entries are restored too. An undo that also cleans
up is not an undo - the operator asked for the previous state, and giving them
a different state leaves them unable to reason about what happened.

The snapshot is the only source of truth for what to put back. Where it holds
no answer - no entry for the pair, a failed read, or a prior array that was
empty - there is nothing to restore, and restore skips the pair. None of those
are a licence to invent a payload: this endpoint replaces the whole policy-1
array, so a guessed write is a destructive one.
"""

import json
import os
from dataclasses import dataclass

from vtex_fixed_price_uploader import writer as writer_module
from vtex_fixed_price_uploader.config import check_account_allowed
from vtex_fixed_price_uploader.pricing import policy1
from vtex_fixed_price_uploader.reader import is_failed_read

# The hashes the restore run records in its log header. Restore has no csv and
# no snapshot digest of its own, but `WriteLog.resume` matches on both, so they
# have to be stable across runs for a halted restore to be resumable - and
# distinct enough that restore rows can never be appended into an open forward
# upload's run.
_LOG_TAG = "restore"


@dataclass(frozen=True)
class RestoreResult:
    """Counts for one restore pass.

    `restored` and `failed` cover only the pairs actually written. Pairs the
    snapshot could not answer for are neither, so a caller reporting to an
    operator should show `len(pairs) - restored - failed` as skipped rather
    than implying every pair was handled.
    """

    restored: int = 0
    failed: int = 0
    halted: str = ""


def pairs_from_log(path):
    """Every pair the log records as successfully written, newest run first."""
    if not os.path.exists(path):
        return []
    runs, current = [], None
    # errors="replace" to match WriteLog's own tolerance: a process killed
    # mid-line can leave a torn multi-byte tail, and a rollback that raises
    # UnicodeDecodeError on the log it is meant to read is a rollback the
    # operator cannot run at exactly the moment they need it.
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue      # a torn write; skip it rather than crash
            if not isinstance(record, dict):
                continue
            kind = record.get("kind")
            if kind == "start":
                current = []
                runs.append(current)
            elif kind == "end":
                current = None
            elif kind == "row" and current is not None \
                    and record.get("status") == 200:
                sku, account = record.get("sku"), record.get("account")
                if sku is None or account is None:
                    continue      # an incomplete record; skip, do not crash
                current.append((sku, account))
    out, seen = [], set()
    for run in reversed(runs):
        for pair in run:
            if pair not in seen:
                seen.add(pair)
                out.append(pair)
    return out


def _halt_message(status, restored):
    prices = "{} price{}".format(restored, "" if restored == 1 else "s")
    if status == 401:
        return ("Your login was rejected part-way through. {} were put back. "
                "Get a fresh login and run this again.".format(prices))
    return ("This login is not permitted to change pricing. {} were put back. "
            "Ask for pricing permission and run this again.".format(prices))


def restore(config, snapshot, pairs, token, log, post=None, progress=None):
    """Re-post each pair's prior policy-1 array from the snapshot."""
    post = post or writer_module.post_fixed
    # The account guard runs here, not only inside `post`, so it holds for
    # every pair regardless of which writer the caller injected, and so a
    # forbidden account is refused before it can be looked up or logged.
    for _sku, account in pairs:
        check_account_allowed(config, account)

    if log.unfinished() is None:
        log.begin(len(pairs), _LOG_TAG, _LOG_TAG)
    else:
        # A halted restore left its run open. Resuming adopts it; a log whose
        # open run is someone else's raises InputsChanged, which is the right
        # loud refusal - restore rows must not be interleaved into a forward
        # upload's run.
        log.resume(_LOG_TAG, _LOG_TAG)

    restored = failed = 0
    halted = ""
    total = len(pairs)

    for index, (sku, account) in enumerate(pairs, start=1):
        record = snapshot.get((sku, account))
        payload = None
        if record is not None:
            status_read, data = record
            if not is_failed_read(status_read) and data is not None:
                payload = policy1(data)

        # Nothing the snapshot can answer for: no entry, a failed read, or a
        # prior array that held no policy-1 entries. The last is not a licence
        # to post []: that would clear every fixed price on the sku, which is
        # a destructive write and not the previous state.
        if not payload:
            if progress:
                progress(index, total)
            continue

        status, _detail = post(config, account, str(sku), payload, token)
        log.append(sku, account, status, len(payload))

        if status in (401, 403):
            halted = _halt_message(status, restored)
            break
        if status == 200:
            restored += 1
        else:
            failed += 1
        if progress:
            progress(index, total)

    if not halted:
        log.finish()
    return RestoreResult(restored=restored, failed=failed, halted=halted)
