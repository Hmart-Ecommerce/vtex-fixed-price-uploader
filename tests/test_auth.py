import base64
import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest

import vtex_fixed_price_uploader.auth as auth
from vtex_fixed_price_uploader.auth import (
    PAIRS_PER_SECOND, SAFETY_MARGIN, TokenExpiringSoon, check_headroom,
    estimate_seconds, token_expiry)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def make_token(exp_dt):
    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return "{}.{}.{}".format(
        seg({"alg": "RS256", "typ": "JWT", "kid": "test-key-2026"}),
        seg({"exp": int(exp_dt.timestamp())}),
        base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode(),
    )


def assert_no_token_material(message, token):
    for segment in token.split("."):
        assert segment not in message
        for start in range(len(segment) - 4):
            assert segment[start:start + 5] not in message


def test_public_functions_have_the_plan_annotations():
    assert token_expiry.__annotations__ == {
        "token": str,
        "return": datetime | None,
    }
    assert estimate_seconds.__annotations__ == {
        "read_pairs": int,
        "write_rows": int,
        "return": float,
    }
    assert check_headroom.__annotations__ == {"return": float | None}


def test_token_expiry_reads_the_exp_claim():
    exp = NOW + timedelta(hours=6)
    got = token_expiry(make_token(exp))
    assert got == exp.replace(microsecond=0)


def test_token_expiry_handles_missing_padding():
    exp = NOW + timedelta(hours=1, seconds=7)
    expected = exp.replace(microsecond=0)

    def seg(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj).encode()).rstrip(b"=").decode()

    header = seg({"alg": "RS256"})
    for target_modulo in (0, 2, 3):
        for filler_length in range(12):
            payload = seg({
                "exp": int(exp.timestamp()),
                "filler": "x" * filler_length,
            })
            if len(payload) % 4 == target_modulo:
                break
        else:
            pytest.fail("could not construct requested payload length")
        assert len(payload) % 4 == target_modulo
        assert token_expiry(f"{header}.{payload}.signature") == expected


def test_token_expiry_returns_none_for_garbage():
    assert token_expiry("not-a-jwt") is None
    assert token_expiry("") is None
    assert token_expiry("a.b.c") is None


def test_token_expiry_propagates_unexpected_internal_errors(monkeypatch):
    token = make_token(NOW + timedelta(hours=1))

    def raise_unexpected_error(_payload):
        raise RuntimeError("unexpected internal error")

    monkeypatch.setattr(auth.json, "loads", raise_unexpected_error)
    with pytest.raises(RuntimeError, match="unexpected internal error"):
        token_expiry(token)


def test_token_expiry_returns_none_when_exp_absent():
    def seg(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj).encode()).rstrip(b"=").decode()
    token = "{}.{}.sig".format(seg({"alg": "x"}), seg({"sub": "someone"}))
    assert token_expiry(token) is None


@pytest.mark.parametrize(
    "exp", [True, "1787000000", 0, -1, float("nan"), float("inf"), -float("inf")])
def test_token_expiry_returns_none_for_invalid_exp_values(exp):
    def seg(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj).encode()).rstrip(b"=").decode()

    token = "{}.{}.signature".format(
        seg({"alg": "RS256"}), seg({"exp": exp}))
    assert token_expiry(token) is None


def test_token_expiry_accepts_a_finite_positive_float():
    def seg(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj).encode()).rstrip(b"=").decode()

    token = "{}.{}.signature".format(
        seg({"alg": "RS256"}), seg({"exp": 1787000000.9}))
    assert token_expiry(token) == datetime(
        2026, 8, 17, 20, 53, 20, 900000, tzinfo=timezone.utc)


def test_estimate_uses_the_deliberate_rounded_rate_and_margin():
    """6.0 is a deliberate round-down from 3,949 / 652 = 6.056."""
    assert PAIRS_PER_SECOND == 6.0
    assert SAFETY_MARGIN == 1.5
    assert estimate_seconds(3949, 0) == 3949 / 6.0 * 1.5


def test_estimate_adds_the_write_phase():
    assert estimate_seconds(100, 100) == (100 / 6.0 + 100 / 6.0) * 1.5


def test_check_headroom_passes_with_a_long_lived_token():
    token = make_token(NOW + timedelta(hours=10))
    assert check_headroom(token, 3949, 3070, NOW) == 34245.25


def test_check_headroom_raises_when_the_token_dies_first():
    token = make_token(NOW + timedelta(minutes=2))
    with pytest.raises(TokenExpiringSoon):
        check_headroom(token, 3949, 3070, NOW)


def test_check_headroom_raises_at_exactly_zero_headroom():
    token = make_token(NOW)
    with pytest.raises(TokenExpiringSoon):
        check_headroom(token, 0, 0, NOW)


def test_check_headroom_distinguishes_expired_and_expiring_logins():
    expired = make_token(NOW - timedelta(hours=1))
    with pytest.raises(TokenExpiringSoon) as expired_error:
        check_headroom(expired, 0, 0, NOW)
    assert str(expired_error.value) == (
        "Your login has already expired. Get a fresh login and start again.")

    expiring = make_token(NOW + timedelta(minutes=2))
    with pytest.raises(TokenExpiringSoon) as expiring_error:
        check_headroom(expiring, 3949, 3070, NOW)
    assert str(expiring_error.value) == (
        "Your login expires in 2 minutes but this run needs about 29 minutes. "
        "Get a fresh login and start again.")


def test_check_headroom_rejects_a_timezone_naive_now():
    token = make_token(NOW + timedelta(hours=1))
    naive_now = datetime(2026, 8, 26, 12, 0)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        check_headroom(token, 10, 10, naive_now)


def test_check_headroom_message_never_contains_the_token():
    token = make_token(NOW + timedelta(minutes=2))
    try:
        check_headroom(token, 3949, 3070, NOW)
    except TokenExpiringSoon as exc:
        assert_no_token_material(str(exc), token)
    else:
        pytest.fail("expected TokenExpiringSoon")


@pytest.mark.parametrize("token", [None, "", "   "])
def test_check_headroom_rejects_an_absent_credential(token):
    with pytest.raises(ValueError, match="credential is required") as caught:
        check_headroom(token, 10, 10, NOW)
    assert str(caught.value) == "A credential is required."


def test_check_headroom_returns_none_when_expiry_is_unknown():
    def seg(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj).encode()).rstrip(b"=").decode()

    without_exp = "{}.{}.signature".format(
        seg({"alg": "RS256"}), seg({"sub": "someone"}))
    for token in ("opaque", without_exp):
        headroom = check_headroom(token, 10, 10, NOW)
        assert headroom is None
        assert headroom != float("inf")
