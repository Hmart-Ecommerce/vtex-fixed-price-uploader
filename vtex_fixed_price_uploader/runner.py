"""Sequence the read-only preflight and the production write phase."""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from vtex_fixed_price_uploader import writer as writer_module
from vtex_fixed_price_uploader.auth import check_headroom
from vtex_fixed_price_uploader.compose import compose
from vtex_fixed_price_uploader.names import fetch_names
from vtex_fixed_price_uploader.parser import parse_csv
from vtex_fixed_price_uploader.reader import read_all, snapshot_hash
from vtex_fixed_price_uploader.report import build_model
from vtex_fixed_price_uploader.rules import blocked_pairs, evaluate
from vtex_fixed_price_uploader.writelog import sha256_of


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
    snapshot_hash: str = ""
    now: datetime = None


@dataclass(frozen=True)
class ApplyResult:
    written: int = 0
    failed: int = 0
    skipped: int = 0
    halted: str = ""


def preflight(config, source, token, now=None, progress=None, fetch=None,
              name_fetch=None):
    """Run phases 0-3: read, validate, and build the report without writing."""
    now = now or datetime.now(timezone.utc)
    rows = parse_csv(source, config)

    skus = {str(row.sku) for row in rows}
    accounts = set(config.accounts.values())
    potential_writes = {(str(row.sku), row.account) for row in rows}
    # When expiry is opaque, the full preflight read below is the credential
    # probe: a rejected credential raises from read_all and is never converted
    # into a per-row finding.
    check_headroom(token, len(skus) * len(accounts), len(potential_writes), now)

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
    model = build_model(findings, compositions, write_pairs, names)

    csv_hash = sha256_of([
        [str(row.sku), row.account, row.promo, row.list_price,
         row.start.isoformat() if row.start else None,
         row.end.isoformat() if row.end else None]
        for row in rows
    ])

    return Preflight(
        rows=rows, reads=reads, compositions=compositions, findings=findings,
        model=model, write_pairs=write_pairs, names=names, csv_hash=csv_hash,
        snapshot_hash=snapshot_hash(reads), now=now)


def apply(config, pre, token, log, progress=None, post=None):
    """Write every eligible pair, halting the whole run on 401 or 403."""
    post = post or writer_module.post_fixed

    if log.unfinished() is None:
        log.begin(len(pre.write_pairs), pre.csv_hash, pre.snapshot_hash)
    else:
        # A newly constructed WriteLog has no in-memory open-run state. Resume
        # validates both input hashes and enables append for this instance.
        log.resume(pre.csv_hash, pre.snapshot_hash)

    already = log.done_pairs()
    targets = sorted(pre.write_pairs)
    written = failed = skipped = 0
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
        else:
            failed += 1
        if progress:
            progress(index, len(targets))

    if not halted:
        log.finish()
    return ApplyResult(written=written, failed=failed, skipped=skipped,
                       halted=halted)
