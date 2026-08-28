"""Where the deployment-specific names live.

The package ships generic. Account names, region codes, and the never-write
list arrive here at runtime, which is what keeps company identifiers out of
the source tree.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


class DisallowedAccount(Exception):
    """Raised before any request is built when an account is not writable."""


@dataclass(frozen=True)
class Config:
    # `accounts` is a read-only view, not a plain dict. `frozen=True` only
    # stops the field being rebound; without the view, an in-place insert
    # would re-open the allowlist at runtime and let a write reach an account
    # that was never configured.
    accounts: Mapping[str, str]
    never_write: tuple[str, ...]
    trade_policy: str
    catalog_host: str | None = None


def load_config(source):
    """Build a Config from a dict or a path to a JSON file."""
    raw = source
    if isinstance(source, str):
        with open(source, encoding="utf-8") as fh:
            raw = json.load(fh)

    if not isinstance(raw, dict):
        raise ValueError("configuration source must be a mapping")

    accounts = raw.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError(
            "config must contain a non-empty 'accounts' mapping of region "
            "code to account name")
    if not all(
        isinstance(region, str)
        and isinstance(account, str)
        and account
        for region, account in accounts.items()
    ):
        raise ValueError(
            "'accounts' must map string region codes to non-empty string "
            "account names")

    regions_by_account: dict[str, list[str]] = {}
    for region, account in accounts.items():
        regions_by_account.setdefault(account, []).append(region)
    duplicates = [
        (account, sorted(regions))
        for account, regions in regions_by_account.items()
        if len(regions) > 1
    ]
    if duplicates:
        account, regions = sorted(duplicates)[0]
        raise ValueError(
            "account {!r} is assigned to multiple region codes: {}; each "
            "account must have exactly one region code".format(
                account, ", ".join(regions)))

    trade_policy = str(raw.get("trade_policy", "1"))
    if trade_policy != "1":
        raise ValueError(
            "this tool only operates on trade policy 1; got {!r}".format(
                trade_policy))

    never_write = raw.get("never_write")
    if never_write is None:
        never_write = ()
    elif (isinstance(never_write, str)
          or not isinstance(never_write, (list, tuple))
          or not all(isinstance(account, str) for account in never_write)):
        raise ValueError(
            "'never_write' must be a list or tuple containing only strings")

    overlap = sorted(set(accounts.values()).intersection(never_write))
    if overlap:
        raise ValueError(
            "account {!r} appears in both accounts and never_write; remove "
            "it from one list".format(overlap[0]))

    # Same strict style as `accounts` and `never_write`: a wrong TYPE here is
    # accepted silently by a truthiness test and then dies mid-run, deep in a
    # worker, with an AttributeError - over a cosmetic product label. An
    # absent, null, or empty value means "no catalog host" and is fine;
    # anything that is not a string is a config error, caught at load.
    catalog_host = raw.get("catalog_host")
    if catalog_host is not None and not isinstance(catalog_host, str):
        raise ValueError(
            "'catalog_host' must be a non-empty string or absent; got "
            "{}".format(type(catalog_host).__name__))

    return Config(
        accounts=MappingProxyType(dict(accounts)),
        never_write=tuple(never_write),
        trade_policy=trade_policy,
        catalog_host=catalog_host or None,
    )


def check_account_allowed(config, account):
    """Refuse loudly unless `account` is writable.

    Two independent conditions, both required. The allowlist alone would be
    enough today, but never_write catches the future edit that adds the master
    account to the allowlist by mistake - a mistake that would otherwise be
    silent and catastrophic.
    """
    if account in config.never_write:
        raise DisallowedAccount(
            "refusing to write to {!r}: listed in never_write".format(account))
    if account not in set(config.accounts.values()):
        raise DisallowedAccount(
            "refusing to write to {!r}: not in the configured accounts".format(
                account))
