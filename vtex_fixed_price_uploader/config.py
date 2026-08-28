"""Where the deployment-specific names live.

The package ships generic. Account names, region codes, and the never-write
list arrive here at runtime, which is what keeps company identifiers out of
the source tree.
"""

import json
from dataclasses import dataclass


class DisallowedAccount(Exception):
    """Raised before any request is built when an account is not writable."""


@dataclass(frozen=True)
class Config:
    accounts: dict
    never_write: tuple
    trade_policy: str


def load_config(source):
    """Build a Config from a dict or a path to a JSON file."""
    raw = source
    if isinstance(source, str):
        with open(source, encoding="utf-8") as fh:
            raw = json.load(fh)

    accounts = raw.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError(
            "config must contain a non-empty 'accounts' mapping of region "
            "code to account name")

    trade_policy = str(raw.get("trade_policy", "1"))
    if trade_policy != "1":
        raise ValueError(
            "this tool only operates on trade policy 1; got {!r}".format(
                trade_policy))

    return Config(
        accounts=dict(accounts),
        never_write=tuple(raw.get("never_write") or ()),
        trade_policy=trade_policy,
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
