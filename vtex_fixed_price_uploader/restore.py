"""Put back exactly what was there before.

Faithful, not tidy: expired entries are restored too. An undo that also cleans
up is not an undo - the operator asked for the previous state, and giving them
a different state leaves them unable to reason about what happened.

The snapshot is the only source of truth for what to put back, and the line
that matters runs between "the snapshot says there was nothing" and "the
snapshot cannot say". A successful read (200) showing an empty policy-1 array
is the first: the sku genuinely had no fixed price, and posting [] puts that
exact state back. No entry for the pair, a 404, or a failed read is the
second: nothing was learned, so the pair is skipped. This endpoint replaces
the whole policy-1 array, so posting [] on the second kind would be a guessed
write, and a guessed write here is a destructive one.
"""

import json
import os
from dataclasses import dataclass

from vtex_fixed_price_uploader import writer as writer_module
from vtex_fixed_price_uploader.config import check_account_allowed
from vtex_fixed_price_uploader.pricing import policy1

# The hashes the restore run records in its log header. Restore has no csv and
# no snapshot digest of its own, but `WriteLog.resume` matches on both, so they
# have to be stable across runs for a halted restore to be resumable - and
# distinct enough that restore rows can never be appended into an open forward
# upload's run.
_LOG_TAG = "restore"


@dataclass(frozen=True)
class RestoreResult:
    """Counts for one restore pass.

    `restored` and `failed` cover only the pairs actually written. `skipped`
    counts the pairs the snapshot could not answer for, and it is reported
    rather than derived: a skipped pair still holds whatever the upload put
    there, so an operator who is told only "restored 0" reads it as "nothing
    happened" when the truth is "your new prices are all still live".

    A halted pass reaches neither counter for the pairs after the halt, so
    `restored + failed + skipped` is less than `len(pairs)` exactly then.
    """

    restored: int = 0
    failed: int = 0
    skipped: int = 0
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

    restored = failed = skipped = 0
    halted = ""
    total = len(pairs)

    for index, (sku, account) in enumerate(pairs, start=1):
        record = snapshot.get((sku, account))
        # None means "the snapshot cannot answer"; [] means "the snapshot
        # answered, and the answer was no fixed prices". Only a 200 produces
        # the second - a 404 is a successful read, but it says the pricing
        # record was not found, not that policy 1 held no entries, and
        # `is_failed_read` deliberately lumps the two successes together.
        payload = None
        if record is not None:
            status_read, data = record
            if status_read == 200 and data is not None:
                payload = policy1(data)

        if payload is None:
            # No entry, a 404, or a failed read. Nothing is put back, and
            # whatever the upload wrote stays live - which is why the caller
            # gets this counted rather than left to infer from a subtraction.
            skipped += 1
            if progress:
                progress(index, total)
            continue

        # `allow_empty` is passed on this path and only this path. An empty
        # payload has been proved from a successful read, so it restores the
        # prior state instead of guessing at one.
        status, _detail = post(config, account, str(sku), payload, token,
                               allow_empty=True)
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
    return RestoreResult(restored=restored, failed=failed, skipped=skipped,
                         halted=halted)
