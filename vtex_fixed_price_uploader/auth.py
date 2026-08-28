"""Refuse to start a run the credential cannot survive.

The exp claim is decoded, never verified - the signature is the server's
business. This is a courtesy check so a long batch does not begin knowing it
will die halfway, which is exactly how 372 writes were lost once.
"""

import base64
import binascii
import json
import math
from datetime import datetime, timezone

PAIRS_PER_SECOND = 6.0    # deliberate round-down from 3,949 / 652 = 6.056
SAFETY_MARGIN = 1.5


class TokenExpiringSoon(Exception):
    """The credential expires before the batch can finish."""


def token_expiry(token: str) -> datetime | None:
    """The exp claim as a UTC datetime, or None if it cannot be read."""
    segments = str(token).split(".")
    if len(segments) < 2:
        return None
    try:
        payload = segments[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims["exp"]
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error):
        return None
    if (isinstance(exp, bool) or not isinstance(exp, (int, float)) or
            not math.isfinite(exp) or exp <= 0):
        return None
    try:
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def estimate_seconds(read_pairs: int, write_rows: int) -> float:
    """How long the whole run should take, generously."""
    raw = (read_pairs / PAIRS_PER_SECOND) + (write_rows / PAIRS_PER_SECOND)
    return raw * SAFETY_MARGIN


def check_headroom(token, read_pairs, write_rows, now) -> float | None:
    """Return seconds to spare, or None when expiry cannot be measured.

    Raises TokenExpiringSoon when there is no headroom. A None return is not a
    pass: the caller MUST fall back to the pre-flight test request. A missing
    credential is a programming error and raises ValueError.
    """
    if token is None or not str(token).strip():
        raise ValueError("A credential is required.")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    expires = token_expiry(token)
    if expires is None:
        return None
    needed = estimate_seconds(read_pairs, write_rows)
    headroom = (expires - now).total_seconds() - needed
    if headroom <= 0:
        if expires <= now:
            raise TokenExpiringSoon(
                "Your login has already expired. "
                "Get a fresh login and start again.")
        raise TokenExpiringSoon(
            "Your login expires in {:.0f} minutes but this run needs about "
            "{:.0f} minutes. Get a fresh login and start again.".format(
                max((expires - now).total_seconds(), 0) / 60, needed / 60))
    return headroom
