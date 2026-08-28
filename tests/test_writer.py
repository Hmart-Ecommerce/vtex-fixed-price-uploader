import dataclasses
import inspect
import io
import json
import socket
import threading
import urllib.error

import pytest

from vtex_fixed_price_uploader import writer
from vtex_fixed_price_uploader.config import DisallowedAccount, load_config

CFG = load_config({"accounts": {"R1": "acct_one"},
                   "never_write": ["acct_master"], "trade_policy": "1"})


class FakeResponse:
    status = 200
    def read(self):
        return b""
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_post_targets_the_fixed_policy_one_endpoint(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = json.loads(req.data.decode())
        return FakeResponse()

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    status, err = writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}],
                                    "tok")
    assert status == 200 and err == ""
    assert seen["url"].endswith("/acct_one/pricing/prices/111/fixed/1")
    assert seen["method"] == "POST"
    assert seen["body"] == [{"value": 1.0}]


def explode(req, timeout=None):
    raise AssertionError("a request must never be built for this input")


def test_an_empty_array_is_refused(monkeypatch):
    """This endpoint replaces the whole policy-1 array.

    `compose()` emits one entry per row, so a non-empty `rows` always yields a
    non-empty array. An empty array can therefore only come from an upstream
    bug, and sending it would clear every fixed price on the sku. This tool
    never wants to clear a sku.
    """
    monkeypatch.setattr(writer.urllib.request, "urlopen", explode)
    with pytest.raises(ValueError, match="payload"):
        writer.post_fixed(CFG, "acct_one", "111", [], "tok")


def test_an_empty_array_is_allowed_only_when_the_caller_opts_in(monkeypatch):
    """Restore, and only restore, may say "this sku had no fixed price".

    An empty array replaces the policy-1 array with nothing. On the apply path
    that can only ever be an upstream bug, so the default stays a refusal. On
    the restore path it is the one and only way to express a prior state of
    "no fixed price at all", which is the commonest thing an undo has to put
    back - the first price ever placed on a sku.
    """
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return FakeResponse()

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    status, err = writer.post_fixed(CFG, "acct_one", "111", [], "tok",
                                    allow_empty=True)
    assert status == 200 and err == ""
    assert seen["body"] == []


def test_opting_in_to_an_empty_array_relaxes_nothing_else(monkeypatch):
    """`allow_empty` waives emptiness only, never the shape of the payload."""
    monkeypatch.setattr(writer.urllib.request, "urlopen", explode)
    for payload in (None, "wipe", {"value": 1.0}, [None]):
        with pytest.raises(ValueError, match="payload"):
            writer.post_fixed(CFG, "acct_one", "111", payload, "tok",
                              allow_empty=True)


def test_the_apply_path_has_no_way_to_opt_in_to_an_empty_array():
    """The opt-in exists for restore; `apply` must never reach it.

    Two halves, because either alone would let the defect back in: `apply`
    refuses an empty composition before it calls the writer at all, and the
    call it does make carries no keyword that would waive the writer's own
    guard.

    Lives here rather than in test_runner.py because it is a property of this
    module's opt-in - the apply-side call site is only the other end of it.
    """
    import io
    from dataclasses import replace
    from datetime import datetime, timezone

    from vtex_fixed_price_uploader.runner import apply, preflight
    from vtex_fixed_price_uploader.writelog import WriteLog

    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    header = ("skuId,listPriceR1,promoPriceR1,dateStartR1,dateEndR1,"
              "promo_type\n")
    row = "111,8.99,7.99,2026-08-28T1:00:00-03:00,2026-09-18T1:00:00-03:00,w\n"

    def fetch(account, sku, token, timeout=30, retries=2):
        return 200, {"basePrice": 8.99, "fixedPrices": []}

    pre = preflight(CFG, io.StringIO(header + row), "tok", now=now,
                    fetch=fetch, name_fetch=lambda url, timeout=30: [])

    seen = []

    def record(config, account, sku, payload, token, **kw):
        seen.append(kw)
        return 200, ""

    log_path = str(tmp_log_dir() / "apply.jsonl")
    apply(CFG, pre, "tok", WriteLog(log_path), fetch=fetch, post=record)
    assert seen, "the fixture wrote nothing, so it proves nothing"
    assert all("allow_empty" not in kw for kw in seen)

    emptied = replace(pre, compositions={
        pair: replace(comp, new_array=[])
        for pair, comp in pre.compositions.items()})
    blocked = []
    with pytest.raises(ValueError, match="empty"):
        apply(CFG, emptied, "tok", WriteLog(str(tmp_log_dir() / "b.jsonl")),
              fetch=fetch,
              post=lambda *a, **k: blocked.append(1) or (200, ""))
    assert blocked == []


def tmp_log_dir():
    import pathlib
    import tempfile
    return pathlib.Path(tempfile.mkdtemp())


@pytest.mark.parametrize("payload", [
    None,
    {"value": 1.0},
    "wipe",
    1.0,
    ({"value": 1.0},),
    [{"value": 1.0}, "wipe"],
    [None],
])
def test_a_payload_that_is_not_a_list_of_entries_is_refused(monkeypatch,
                                                            payload):
    monkeypatch.setattr(writer.urllib.request, "urlopen", explode)
    with pytest.raises(ValueError, match="payload"):
        writer.post_fixed(CFG, "acct_one", "111", payload, "tok")


@pytest.mark.parametrize("sku", [
    "111/fixed/2",
    "../../other_acct/pricing/prices/999",
    "",
    "1 1?x=y",
    "111?policy=2",
    "111#frag",
    None,
    111,
])
def test_a_sku_that_is_not_a_plain_identifier_is_refused(monkeypatch, sku):
    """The account has an exact-match allowlist; the sku had nothing.

    Sku values originate in a spreadsheet cell and are interpolated straight
    into the production write URL, so anything that could change the path,
    add a query, or traverse out of the sku segment has to be refused before
    interpolation.
    """
    monkeypatch.setattr(writer.urllib.request, "urlopen", explode)
    with pytest.raises(ValueError, match="sku"):
        writer.post_fixed(CFG, "acct_one", sku, [{"value": 1.0}], "tok")


def test_a_config_carrying_another_trade_policy_is_refused(monkeypatch):
    """`load_config` is not the only way to build a Config.

    `dataclasses.replace` and a direct constructor call both bypass it, so
    "policy is always 1" has to be asserted in the module that does the write.
    """
    monkeypatch.setattr(writer.urllib.request, "urlopen", explode)
    rogue = dataclasses.replace(CFG, trade_policy="2")
    with pytest.raises(ValueError, match="trade policy"):
        writer.post_fixed(rogue, "acct_one", "111", [{"value": 1.0}], "tok")


def test_master_account_is_refused_before_any_request(monkeypatch):
    monkeypatch.setattr(writer.urllib.request, "urlopen", explode)
    with pytest.raises(DisallowedAccount):
        writer.post_fixed(CFG, "acct_master", "111", [], "tok")


def test_unknown_account_is_refused(monkeypatch):
    monkeypatch.setattr(writer.urllib.request, "urlopen", explode)
    with pytest.raises(DisallowedAccount):
        writer.post_fixed(CFG, "acct_stranger", "111", [{"value": 1.0}], "tok")


def test_the_account_guard_runs_before_the_url_is_built(monkeypatch):
    """The docstring's central claim, pinned.

    A refused account must never reach string interpolation, so the guard call
    has to be observably ordered before the URL is assembled - not merely
    present somewhere in the function.
    """
    sequence = []

    class RecordingHost(str):
        def __format__(self, spec):
            sequence.append("url-built")
            return str.__format__(str(self), spec)

    real_guard = writer.check_account_allowed

    def recording_guard(config, account):
        sequence.append("guard")
        return real_guard(config, account)

    monkeypatch.setattr(writer, "check_account_allowed", recording_guard)
    monkeypatch.setattr(writer, "PRICING_HOST",
                        RecordingHost(writer.PRICING_HOST))
    monkeypatch.setattr(writer.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResponse())

    writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}], "tok")
    assert sequence == ["guard", "url-built"]


def test_no_parameter_can_bypass_the_account_guard(monkeypatch):
    """No dry-run, admin or force switch may be added to this signature.

    An override flag is the shape a future caller would reach for, and it
    would ship green against every behavioural test here, so the signature
    itself is the thing under test.

    `allow_empty` is the one waiver this signature carries, and it is listed
    here deliberately rather than tolerated: it waives emptiness for the
    restore path and nothing else. The second half proves it - a forbidden
    account is still refused with the flag set, so it is not the beginnings of
    a force switch.
    """
    signature = inspect.signature(writer.post_fixed)
    assert list(signature.parameters) == [
        "config", "account", "sku", "payload", "token", "timeout", "retries",
        "allow_empty",
    ]
    assert not any(
        parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
        for parameter in signature.parameters.values()
    )

    monkeypatch.setattr(writer.urllib.request, "urlopen", explode)
    with pytest.raises(DisallowedAccount):
        writer.post_fixed(CFG, "acct_master", "111", [], "tok",
                          allow_empty=True)


def test_a_mutated_allowlist_cannot_open_an_unknown_account(monkeypatch):
    """`Config` is frozen, but a live dict inside it would not be."""
    monkeypatch.setattr(writer.urllib.request, "urlopen", explode)
    config = load_config({"accounts": {"R1": "acct_one"},
                          "never_write": ["acct_master"], "trade_policy": "1"})
    with pytest.raises(TypeError):
        config.accounts["R9"] = "acct_stranger"
    with pytest.raises(DisallowedAccount):
        writer.post_fixed(config, "acct_stranger", "111", [{"value": 1.0}],
                          "tok")


def test_401_is_returned_not_retried(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                     None)

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    status, err = writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}],
                                    "tok", retries=3)
    assert status == 401
    assert len(calls) == 1


def test_500_is_retried_then_returned(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {},
                                     None)

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(writer, "_sleep", lambda seconds: None)
    status, _ = writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}],
                                  "tok", retries=2)
    assert status == 500
    assert len(calls) == 3


def test_network_failure_returns_zero(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(writer, "_sleep", lambda seconds: None)
    status, err = writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}],
                                    "tok", retries=0)
    assert status == 0
    assert "connection reset" in err


def test_response_phase_network_failure_retries_then_returns_zero(monkeypatch):
    attempts = []
    ready = threading.Event()

    def accept_posts(listener):
        ready.set()
        for _ in range(2):
            connection, _ = listener.accept()
            with connection:
                received = b""
                while b"\r\n\r\n" not in received:
                    received += connection.recv(4096)
                headers, body = received.split(b"\r\n\r\n", 1)
                content_length = next(
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                while len(body) < content_length:
                    body += connection.recv(4096)
                attempts.append(body[:content_length])

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        worker = threading.Thread(target=accept_posts, args=(listener,))
        worker.start()
        ready.wait()
        host, port = listener.getsockname()
        monkeypatch.setattr(writer, "PRICING_HOST", f"http://{host}:{port}")
        monkeypatch.setattr(writer, "_sleep", lambda seconds: None)

        status, error = writer.post_fixed(
            CFG, "acct_one", "111", [{"value": 1.0}], "sensitive-token",
            retries=1,
        )
        worker.join(timeout=2)

    assert status == 0
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert error
    assert "sensitive-token" not in error


def test_token_travels_in_the_header_only(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        seen["body"] = req.data.decode()
        return FakeResponse()

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}],
                      "sensitive-token")
    assert "sensitive-token" not in seen["url"]
    assert "sensitive-token" not in seen["body"]
    assert seen["headers"]["Vtexidclientautcookie"] == "sensitive-token"


def test_403_is_returned_not_retried(monkeypatch):
    """An appkey that authenticates but lacks pricing permission.

    Same operator-visible symptom as a bad token, so it gets the same
    handling: halt on the first one instead of retrying and then producing N
    identical per-row failures for the rest of the run.
    """
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(writer, "_sleep", lambda seconds: None)
    status, err = writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}],
                                    "tok", retries=3)
    assert status == 403
    assert len(calls) == 1
    assert err


@pytest.mark.parametrize("code", [400, 401, 403, 429, 500])
def test_the_token_never_appears_in_an_http_error_return(monkeypatch, code):
    """Every error path, not only the 200 path.

    `detail` is unscrubbed remote body text headed for operator-facing output,
    and a proxy answering with the offending request header echoed back is the
    realistic way the token gets into it.
    """
    token = "sensitive-token"

    def fake_urlopen(req, timeout=None):
        echo = b"rejected header VtexIdclientAutCookie: " + token.encode()
        raise urllib.error.HTTPError(req.full_url, code, "nope", {},
                                     io.BytesIO(echo))

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(writer, "_sleep", lambda seconds: None)
    status, err = writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}],
                                    token, retries=1)
    assert status == code
    assert token not in err


def test_the_token_never_appears_in_a_network_error_return(monkeypatch):
    token = "sensitive-token"

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("reset while sending " + token)

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(writer, "_sleep", lambda seconds: None)
    status, err = writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}],
                                    token, retries=0)
    assert status == 0
    assert token not in err


def test_the_timeout_reaches_urlopen(monkeypatch):
    """Without this, a production write can hang with no ceiling."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}], "tok",
                      timeout=7)
    assert seen["timeout"] == 7
    writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}], "tok")
    assert seen["timeout"] == 30


def test_every_attempt_sends_identical_bytes(monkeypatch):
    """Retry idempotency.

    This endpoint replaces the whole policy-1 array, so a retry that sent a
    different body would not be a repeat of the same write. Building the body
    once, outside the loop, is what guarantees that; this pins it.
    """
    bodies = []

    def fake_urlopen(req, timeout=None):
        bodies.append(req.data)
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {},
                                     None)

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(writer, "_sleep", lambda seconds: None)
    writer.post_fixed(CFG, "acct_one", "111",
                      [{"value": 1.0}, {"value": 2.0}], "tok", retries=2)
    assert len(bodies) == 3
    assert len(set(bodies)) == 1


def test_the_retry_backoff_is_patchable_and_not_slept_after_the_last_attempt(
        monkeypatch):
    slept = []

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {},
                                     None)

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(writer, "_sleep", lambda seconds: slept.append(seconds))
    writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}], "tok",
                      retries=2)
    assert slept == [writer.RETRY_BACKOFF_SECONDS] * 2


def test_retries_counts_retries_not_attempts(monkeypatch):
    """`retries=1` is two attempts. The docstring now says so; this pins it."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {},
                                     None)

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(writer, "_sleep", lambda seconds: None)
    writer.post_fixed(CFG, "acct_one", "111", [{"value": 1.0}], "tok")
    assert len(calls) == 2


def test_post_fixed_declares_its_return_type():
    assert inspect.signature(writer.post_fixed).return_annotation == \
        tuple[int, str]
