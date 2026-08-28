"""Sequence the read-only preflight and the production write phase."""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from vtex_fixed_price_uploader import writer as writer_module
from vtex_fixed_price_uploader.auth import check_headroom
from vtex_fixed_price_uploader.compose import compose
from vtex_fixed_price_uploader.config import check_account_allowed
from vtex_fixed_price_uploader.names import fetch_names
from vtex_fixed_price_uploader.parser import parse_csv
from vtex_fixed_price_uploader.pricing import fetch_prices
from vtex_fixed_price_uploader.reader import (
    payload_snapshot_hash, read_all, resume_pairs_hash, sha256_of)
from vtex_fixed_price_uploader.report import build_model
from vtex_fixed_price_uploader.rules import blocked_pairs, evaluate
from vtex_fixed_price_uploader.writelog import InputsChanged


@dataclass(frozen=True)
class Preflight:
    rows: list = field(default_factory=list)
    reads: dict = field(default_factory=dict)
    compositions: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    model: object = None
    write_pairs: frozenset = frozenset()
    names: dict = field(default_factory=dict)
    csv_hash: str = ""
    payload_snapshot_hash: str = ""
    resume_pairs_hash: str = ""
    now: datetime = None


@dataclass(frozen=True)
class ApplyResult:
    """Write counts; status 0 is unknown because the write may have landed."""

    written: int = 0
    failed: int = 0
    unknown: int = 0
    skipped: int = 0
    halted: str = ""


PROBE_SKU = "1"


class CredentialRejected(Exception):
    """The credential was refused by VTEX."""


def check_credential(config, token, fetch=None):
    """Raise CredentialRejected when a probe returns 401 or 403.

    A 404 proves the credential works; the probe SKU may simply not exist in
    that account. A 403 proves authentication but also proves this login lacks
    pricing read permission. Error messages never contain the token.
    """
    fetch = fetch or fetch_prices
    account = sorted(config.accounts.values())[0]
    status, _data = fetch(account, PROBE_SKU, token)
    if status == 401:
        raise CredentialRejected(
            "Your login was not accepted. Get a fresh login and try again.")
    if status == 403:
        raise CredentialRejected(
            "This login cannot read pricing for account {}; re-running will "
            "not help; ask for pricing read permission.".format(account))


def preflight(config, source, token, now=None, progress=None, fetch=None,
              name_fetch=None, skip_credential_check=False):
    """Run phases 0-3: read, validate, and build the report without writing."""
    now = now or datetime.now(timezone.utc)
    rows = parse_csv(source, config)

    if not skip_credential_check:
        pairs = len(set(r.sku for r in rows)) * len(
            set(config.accounts.values()))
        check_headroom(token, pairs, len(rows), now)
        check_credential(config, token, fetch=fetch)

    skus = {str(row.sku) for row in rows}

    reads = read_all(config, skus, token, progress=progress, fetch=fetch)

    compositions = {}
    for row in rows:
        key = (str(row.sku), row.account)
        if key in compositions:
            continue
        pair_rows = [candidate for candidate in rows
                     if (str(candidate.sku), candidate.account) == key]
        composition = compose(
            pair_rows, reads.get(key, (0, None))[1], now)
        # CSV-created entries do not need the policy id to compose, but the
        # read-back API includes it and verification filters by it. Normalise
        # the runner-owned copies so an exact read-back remains comparable.
        for entry in composition.new_array:
            entry.setdefault("tradePolicyId", config.trade_policy)
        compositions[key] = composition

    findings = evaluate(rows, reads, compositions, config, now)
    blocked = blocked_pairs(findings)
    write_pairs = frozenset(
        key for key in compositions if key not in blocked)

    names = fetch_names(config, skus, fetch=name_fetch)
    # The file's real row count. `compositions` is keyed by pair, so its
    # length silently merges the several rows a pair may legally carry.
    model = build_model(findings, compositions, write_pairs, names,
                        total_rows=len(rows))

    csv_hash = sha256_of([
        [str(row.sku), row.account, row.promo, row.list_price,
         row.start.isoformat() if row.start else None,
         row.end.isoformat() if row.end else None]
        for row in rows
    ])

    return Preflight(
        rows=rows, reads=reads, compositions=compositions, findings=findings,
        model=model, write_pairs=write_pairs, names=names, csv_hash=csv_hash,
        payload_snapshot_hash=payload_snapshot_hash(reads),
        resume_pairs_hash=resume_pairs_hash(reads), now=now)


def apply(config, pre, token, log, progress=None, post=None, fetch=None,
          abandon_unfinished=False):
    """Write every eligible pair, halting the whole run on 401 or 403.

    The credential is re-probed before the log opens so a login that expired
    during preflight cannot allow any write to begin.
    """
    post = post or writer_module.post_fixed
    pending = log.unfinished()
    if pending is not None and abandon_unfinished:
        already_written = len(log.done_pairs())
        rows = "row" if already_written == 1 else "rows"
        log.discard()
        return ApplyResult(halted=(
            "Abandoned the unfinished upload log. {} {} already written "
            "stay written in VTEX. Run Apply again to start a new "
            "upload.".format(already_written, rows)))

    targets = sorted(pre.write_pairs)
    # Keep the invariant inside apply as well as inside the default writer so
    # an injected writer cannot bypass the account allowlist. Check every
    # target before credential work or a write-log start record.
    for _sku, account in targets:
        check_account_allowed(config, account)

    try:
        check_credential(config, token, fetch=fetch)
    except CredentialRejected as exc:
        return ApplyResult(halted=str(exc))

    if pending is None:
        already = set()
    else:
        already = log.done_pairs()
        already_written = len(already)
        rows = "row" if already_written == 1 else "rows"
        # A newly constructed WriteLog has no in-memory open-run state. Resume
        # validates both input hashes and enables append for this instance.
        try:
            log.resume(pre.csv_hash, pre.resume_pairs_hash)
        except InputsChanged as exc:
            return ApplyResult(halted=(
                "Resume is not possible: {} Abandoning means {} {} already "
                "written stay written in VTEX. To abandon this log, run "
                "Apply with abandon_unfinished=True.".format(
                    exc, already_written, rows)))

    remaining_writes = sum(
        1 for sku, account in targets
        if (str(sku), account) not in already)
    check_headroom(
        token, 0, remaining_writes, datetime.now(timezone.utc))

    if pending is None:
        log.begin(len(pre.write_pairs), pre.csv_hash, pre.resume_pairs_hash)

    written = failed = unknown = skipped = 0
    halted = ""

    for index, (sku, account) in enumerate(targets, start=1):
        string_sku = str(sku)
        if (string_sku, account) in already:
            skipped += 1
            if progress:
                progress(index, len(targets))
            continue

        payload = list(pre.compositions[(sku, account)].new_array)
        if not payload:
            raise ValueError(
                "refusing to write an empty fixed-price payload for SKU {} "
                "in account {}".format(string_sku, account))

        status, detail = post(
            config, account, string_sku, payload, token)
        log.append(string_sku, account, status, len(payload))

        if status in (401, 403):
            halted = (
                "Your login was rejected or is not permitted on pricing "
                "part-way through. {} price{} were written. Get a fresh "
                "login with pricing access and run this again to finish the "
                "rest.".format(written, "" if written == 1 else "s"))
            break
        if status == 200:
            written += 1
        elif status == 0:
            unknown += 1
        else:
            failed += 1
        if progress:
            progress(index, len(targets))

    if not halted:
        log.finish()
    return ApplyResult(written=written, failed=failed, unknown=unknown,
                       skipped=skipped, halted=halted)
