import ast
import importlib
import inspect
import sys

import vtex_fixed_price_uploader.notebook_ui as ui
from vtex_fixed_price_uploader.notebook_ui import AckState
from vtex_fixed_price_uploader.report import ReportModel
from vtex_fixed_price_uploader.verify import VerifyResult


def model(ack_keys=("W1-111",), typed=False, write_rows=10):
    return ReportModel(write_rows=write_rows, ack_keys=ack_keys,
                       needs_typed_confirmation=typed)


def test_not_ready_before_the_warnings_are_acknowledged():
    state = AckState(model())
    assert state.ready is False


def test_one_acknowledgement_covers_every_warning_group():
    """Reviewing the warnings is one decision, so it takes one confirmation.

    The items themselves stay in the report, in full. A tick per group was a
    tick per group unread, and it pushed the upload button off the screen.
    """
    state = AckState(model(ack_keys=("W1-111", "W6-222")))
    assert state.ready is False
    state.acknowledge()
    assert state.ready is True


def test_withdrawing_the_acknowledgement_removes_readiness():
    state = AckState(model())
    state.acknowledge()
    state.acknowledge(False)
    assert state.ready is False


def test_ready_immediately_when_there_is_nothing_to_acknowledge():
    assert AckState(model(ack_keys=())).ready is True


def test_never_ready_with_nothing_to_write():
    state = AckState(model(ack_keys=(), write_rows=0))
    assert state.ready is False
    assert "nothing" in state.blocking_reason.lower()


def test_typed_confirmation_is_required_for_a_large_clean_batch():
    state = AckState(model(ack_keys=(), typed=True, write_rows=600))
    assert state.ready is False
    state.set_typed("600")
    assert state.ready is True


def test_typed_confirmation_rejects_the_wrong_number():
    state = AckState(model(ack_keys=(), typed=True, write_rows=600))
    state.set_typed("599")
    assert state.ready is False


def test_typed_confirmation_tolerates_separators_and_spaces():
    state = AckState(model(ack_keys=(), typed=True, write_rows=2984))
    state.set_typed(" 2,984 ")
    assert state.ready is True


def test_blocking_reason_counts_the_items_waiting_to_be_read():
    state = AckState(model(ack_keys=("W1-111", "W6-222")))
    assert "2" in state.blocking_reason


def test_blocking_reason_reads_as_one_item_when_there_is_one():
    state = AckState(model(ack_keys=("W1-111",)))
    assert "1 item above" in state.blocking_reason


def test_ipywidgets_import_is_lazy_and_package_imports_without_it(monkeypatch):
    """The notebook extra must not be required by the package test suite."""
    source = inspect.getsource(ui)
    assert "import ipywidgets" in source

    tree = ast.parse(source)
    functions = [node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    import_lines = [number for number, line in enumerate(source.splitlines(), 1)
                    if "import ipywidgets" in line]
    assert import_lines
    assert all(any(function.lineno <= line <= function.end_lineno
                   for function in functions)
               for line in import_lines)

    monkeypatch.setitem(sys.modules, "ipywidgets", None)
    importlib.reload(ui)
    assert ui.AckState(model()).ready is False


def verification_result():
    return VerifyResult(
        matched=1,
        mismatched=2,
        still_multiple=1,
        confirmed_empty=1,
        unreadable=1,
        rows=(
            {"verdict": "match", "live_entries": 1},
            {"verdict": "mismatch", "live_entries": 1},
            {"verdict": "mismatch", "live_entries": 2},
            {"verdict": "confirmed_empty", "status": 404},
            {"verdict": "unreadable", "status": 429},
        ),
    )


def test_verification_summary_visibly_accounts_for_every_attempted_row():
    summary = ui.verification_summary(verification_result())
    assert ("5 attempted = 1 matched + 1 mismatched + 1 still multiple + "
            "1 confirmed empty + 1 unreadable") in summary.lower()


def test_verification_summary_reports_empty_and_unreadable_as_problems():
    summary = ui.verification_summary(verification_result()).lower()
    assert "confirmed failure" in summary
    assert "price did not land" in summary
    assert "open question" in summary
    assert "could not be checked" in summary
    assert "re-run the verification" in summary



# --- Fix wave D ---------------------------------------------------------
#
# The screen is driven for real here. The previous test for this behaviour
# grepped run_interactive's source for a literal call and passed whether or
# not the line could ever be reached; these fakes stand in for ipywidgets so
# the callbacks actually run.

import types
from types import SimpleNamespace

import pytest

from vtex_fixed_price_uploader import reader as reader_module
from vtex_fixed_price_uploader import restore as restore_module
from vtex_fixed_price_uploader import runner as runner_module
from vtex_fixed_price_uploader import verify as verify_module
from vtex_fixed_price_uploader import writelog as writelog_module
from vtex_fixed_price_uploader.auth import TokenExpiringSoon
from vtex_fixed_price_uploader.compose import Composition
from vtex_fixed_price_uploader.config import DisallowedAccount
from vtex_fixed_price_uploader.reader import (
    AuthenticationError, UnhealthySnapshot)
from vtex_fixed_price_uploader.report import build_model
from vtex_fixed_price_uploader.restore import RestoreResult
from vtex_fixed_price_uploader.rules import Finding
from vtex_fixed_price_uploader.runner import ApplyResult, CredentialRejected
from vtex_fixed_price_uploader.writelog import (
    InputsChanged, NoOpenRun, UnfinishedRun)


def _text(obj):
    return getattr(obj, "data", None) or str(obj)


class FakeWidget:
    def __init__(self, *args, **kwargs):
        self.children = tuple(args[0]) if args else ()
        self.value = kwargs.pop("value", "")
        self.description = kwargs.pop("description", "")
        self.disabled = kwargs.pop("disabled", False)
        for key, value in kwargs.items():
            setattr(self, key, value)
        self._clicks = []
        self._observers = []

    def observe(self, handler, names=None):
        self._observers.append(handler)

    def on_click(self, handler):
        self._clicks.append(handler)

    def click(self):
        assert self._clicks, "this widget has no click handler"
        for handler in list(self._clicks):
            handler(self)

    def set(self, value):
        self.value = value
        for handler in list(self._observers):
            handler({"name": "value", "new": value})


class FakeOutput(FakeWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lines = []
        self.clears = 0

    def clear_output(self, *args, **kwargs):
        # Real ipywidgets also drops the widgets displayed into this output;
        # the fake counts the call instead, which is the part the screen
        # controls and the part a test can hold it to.
        self.clears += 1
        self.lines.clear()

    def append_display_data(self, obj):
        self.lines.append(_text(obj))

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class Screen:
    """Everything the operator can read, and the widgets they can press."""

    def __init__(self, ui, displayed_text, displayed_widgets):
        self.ui = ui
        self.displayed_text = displayed_text
        self.displayed_widgets = displayed_widgets

    def _all(self):
        found = []

        def walk(widget):
            if not isinstance(widget, FakeWidget) or widget in found:
                return
            found.append(widget)
            for child in getattr(widget, "children", ()):
                walk(child)

        for widget in list(self.ui.values()) + list(self.displayed_widgets):
            walk(widget)
        return found

    @property
    def text(self):
        parts = list(self.ui["output"].lines) + list(self.displayed_text)
        for widget in self._all():
            if type(widget).__name__ == "Password":
                continue      # never read the login off the screen
            for attribute in ("description", "value"):
                value = getattr(widget, attribute, None)
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)

    def widgets(self, kind):
        return [w for w in self._all() if type(w).__name__ == kind]

    def button(self, needle):
        # Case-sensitive: "Upload 2 prices" and "Put the previous prices back" would
        # otherwise both answer to "upload".
        matches = [w for w in self.widgets("Button")
                   if needle in str(w.description)]
        assert matches, "no button matching {!r}; screen has {}".format(
            needle, [w.description for w in self.widgets("Button")])
        return matches[0]


def install_fake_widgets(monkeypatch):
    """Stand in for ipywidgets and IPython.display; return the display sink."""
    displayed_text, displayed_widgets = [], []
    widgets_module = types.ModuleType("ipywidgets")
    for name in ("FileUpload", "Password", "Checkbox", "Button", "IntProgress",
                 "Label", "Text", "HBox", "VBox", "HTML"):
        setattr(widgets_module, name, type(name, (FakeWidget,), {}))
    widgets_module.Output = FakeOutput
    # Layout carries no behaviour the screen depends on - it only stops a
    # widget label being cut to the widget's width - so a bag of attributes
    # is a faithful enough stand-in.
    widgets_module.Layout = type("Layout", (FakeWidget,), {})

    display_module = types.ModuleType("IPython.display")

    class HTML:
        def __init__(self, data):
            self.data = data

    def display(*objs):
        for obj in objs:
            if isinstance(obj, FakeWidget):
                displayed_widgets.append(obj)
            else:
                displayed_text.append(_text(obj))

    display_module.HTML = HTML
    display_module.display = display
    ipython_module = types.ModuleType("IPython")
    ipython_module.display = display_module

    monkeypatch.setitem(sys.modules, "ipywidgets", widgets_module)
    monkeypatch.setitem(sys.modules, "IPython", ipython_module)
    monkeypatch.setitem(sys.modules, "IPython.display", display_module)
    return displayed_text, displayed_widgets


def a_config():
    return SimpleNamespace(accounts={"R1": "acct_one", "R2": "acct_two"},
                           never_write=("acct_master",), trade_policy="1")


def a_preflight(findings=()):
    pairs = {("1001", "acct_one"), ("1002", "acct_two")}
    comps = {pair: Composition(new_array=[{"value": 1}], dropped=[], kept=[])
             for pair in pairs}
    return SimpleNamespace(
        reads={}, findings=list(findings), names={}, write_pairs=pairs,
        compositions=comps,
        model=build_model(list(findings), comps, pairs, {}))


def a_check():
    return verification_result()


class FakeLog:
    def __init__(self, path, *args, **kwargs):
        self.path = path


def open_screen(monkeypatch, tmp_path, findings=(),
                apply_result=None, preflight_error=None,
                restore_result=None, snapshot=None, pairs=None, folder=None):
    """Build the real UI on fake widgets and return it with a call record."""
    displayed_text, displayed_widgets = install_fake_widgets(monkeypatch)
    calls = {"apply": [], "verify": [], "restore": [], "logs": []}
    pre = a_preflight(findings)

    def fake_preflight(config, source, token, **kwargs):
        if preflight_error is not None:
            raise preflight_error
        return pre

    def fake_apply(config, preflight_result, token, log, progress=None,
                   post=None, fetch=None, abandon_unfinished=False):
        calls["apply"].append({"abandon_unfinished": abandon_unfinished})
        return apply_result or ApplyResult(written=2)

    def fake_verify(config, preflight_result, token, **kwargs):
        calls["verify"].append(True)
        return a_check()

    def fake_restore(config, snap, restore_pairs, token, log, post=None,
                     progress=None):
        calls["restore"].append(list(restore_pairs))
        return restore_result or RestoreResult(restored=2)

    def fake_log(path, *args, **kwargs):
        calls["logs"].append(path)
        return FakeLog(path)

    monkeypatch.setattr(runner_module, "preflight", fake_preflight)
    monkeypatch.setattr(runner_module, "apply", fake_apply)
    monkeypatch.setattr(verify_module, "verify", fake_verify)
    monkeypatch.setattr(restore_module, "restore", fake_restore)
    monkeypatch.setattr(restore_module, "pairs_from_log",
                        lambda path: list(pairs if pairs is not None
                                          else [("1001", "acct_one")]))
    monkeypatch.setattr(reader_module, "load_snapshot",
                        lambda path: dict(snapshot if snapshot is not None
                                          else {("1001", "acct_one"):
                                                (200, {"itemId": "1001"})}))
    monkeypatch.setattr(reader_module, "save_snapshot",
                        lambda reads, path: {})
    monkeypatch.setattr(writelog_module, "WriteLog", fake_log)

    config = a_config()
    log_path = str(tmp_path / "write-log.jsonl")
    snapshot_path = str(tmp_path / "snapshot.json")
    widgets_ui = ui.build_ui(config, log_path, snapshot_path)
    ui.run_interactive(config, widgets_ui, folder=folder)
    screen = Screen(widgets_ui, displayed_text, displayed_widgets)
    screen.pre = pre
    screen.calls = calls
    screen.log_path = log_path
    screen.snapshot_path = snapshot_path
    return screen


def start_a_run(screen):
    screen.ui["upload"].value = [{"content": b"sku,acct\n"}]
    screen.ui["token"].value = "a-login-value"
    screen.ui["start"].click()
    return screen


# --- 1. a halt must still be read back, and rows must be accounted for ---

def test_halted_apply_still_verifies_and_summarises():
    said = []
    result = ApplyResult(written=349, failed=20, skipped=0,
                         halted="Your login was rejected part-way through.")
    seen = []
    ui.apply_and_verify(
        None, SimpleNamespace(), "t", None, said.append,
        apply_fn=lambda *a, **k: result,
        verify_fn=lambda *a, **k: (seen.append(True) or verification_result()))
    assert seen == [True], "a halted run skipped the read-back"
    assert any("attempted =" in line for line in said)


def test_row_failures_are_reported_on_the_success_path():
    said = []
    ui.apply_and_verify(
        None, SimpleNamespace(), "t", None, said.append,
        apply_fn=lambda *a, **k: ApplyResult(written=349, failed=20,
                                             skipped=3),
        verify_fn=lambda *a, **k: verification_result())
    text = " ".join(said)
    assert "20" in text and "failed" in text.lower()
    assert "3" in text and "skipped" in text.lower()


def test_row_failures_are_reported_on_the_halted_path():
    said = []
    ui.apply_and_verify(
        None, SimpleNamespace(), "t", None, said.append,
        apply_fn=lambda *a, **k: ApplyResult(written=1, failed=7, skipped=2,
                                             halted="Halted."),
        verify_fn=lambda *a, **k: verification_result())
    text = " ".join(said).lower()
    assert "7" in text and "failed" in text
    assert "2" in text and "skipped" in text


def test_a_clean_run_still_states_the_failure_counts():
    said = []
    ui.apply_and_verify(
        None, SimpleNamespace(), "t", None, said.append,
        apply_fn=lambda *a, **k: ApplyResult(written=5),
        verify_fn=lambda *a, **k: verification_result())
    text = " ".join(said).lower()
    assert "0 rows failed" in text


def test_interactive_halted_upload_still_verifies(monkeypatch, tmp_path):
    screen = open_screen(
        monkeypatch, tmp_path,
        apply_result=ApplyResult(written=349, failed=20,
                                 halted="Your login was rejected part-way "
                                        "through."))
    start_a_run(screen)
    screen.button("Upload").click()
    assert screen.calls["verify"] == [True], "the halt path skipped verify"
    assert "rejected part-way" in screen.text
    assert "attempted =" in screen.text
    assert "20" in screen.text


def test_the_screen_offers_a_verification_only_action(monkeypatch, tmp_path):
    screen = open_screen(monkeypatch, tmp_path,
                         apply_result=ApplyResult(written=1, halted="Halted."))
    start_a_run(screen)
    screen.button("Upload").click()
    screen.calls["verify"].clear()
    screen.button("Check what landed").click()
    assert screen.calls["verify"] == [True]


def test_abandoning_the_unfinished_log_is_never_silent(monkeypatch, tmp_path):
    screen = open_screen(monkeypatch, tmp_path)
    start_a_run(screen)
    screen.button("Upload").click()
    assert screen.calls["apply"] == [{"abandon_unfinished": False}]


def test_abandoning_the_unfinished_log_is_an_operator_choice(monkeypatch,
                                                             tmp_path):
    screen = open_screen(
        monkeypatch, tmp_path,
        apply_result=ApplyResult(halted="Abandoned the unfinished upload "
                                        "log. 349 rows already written stay "
                                        "written in VTEX."))
    start_a_run(screen)
    abandon = [w for w in screen.widgets("Checkbox")
               if "abandon" in str(w.description).lower()]
    assert abandon, "no way for the operator to abandon a wedged log"
    abandon[0].set(True)
    screen.button("Upload").click()
    assert screen.calls["apply"][-1]["abandon_unfinished"] is True
    assert "stay written" in screen.text


# --- 2. no traceback reaches the operator -------------------------------

@pytest.mark.parametrize("error", [
    ValueError("unrecognised date format: '13/13/2026'"),
    ValueError("ambiguous price value '1.234,5'"),
    TokenExpiringSoon("Your login expires in 3 minutes but this run needs "
                      "about 9 minutes. Get a fresh login and start again."),
    AuthenticationError("the pricing API rejected the credential"),
    UnhealthySnapshot("refusing to write a snapshot: 40% of 10 pairs failed"),
    UnfinishedRun("a previous run is still open"),
    NoOpenRun("there is no open run"),
    InputsChanged("the open run used a different csv"),
    DisallowedAccount("refusing to write to 'acct_master'"),
    CredentialRejected("Your login was not accepted."),
    RuntimeError("something nobody predicted"),
])
def test_every_failure_renders_as_a_sentence_with_a_next_step(error):
    sentence = ui.friendly_error(error)
    assert sentence and sentence.strip().endswith(".")
    assert "Traceback" not in sentence
    assert any(word in sentence.lower()
               for word in ("again", "ask ", "send ", "check "))


def test_friendly_error_never_echoes_the_login():
    token = "eyJhbGciOi.super-secret.value"
    sentence = ui.friendly_error(
        RuntimeError("request failed for {}".format(token)), token=token)
    assert token not in sentence


def test_a_bad_date_never_reaches_the_operator_as_a_traceback(monkeypatch,
                                                              tmp_path):
    screen = open_screen(monkeypatch, tmp_path, preflight_error=ValueError(
        "unrecognised date format: '13/13/2026'"))
    start_a_run(screen)
    assert "13/13/2026" in screen.text
    assert "again" in screen.text.lower()


def test_an_unpredicted_failure_still_renders_a_sentence(monkeypatch,
                                                         tmp_path):
    screen = open_screen(monkeypatch, tmp_path,
                         preflight_error=RuntimeError("boom"))
    start_a_run(screen)
    assert "boom" in screen.text
    assert screen.text.strip()


def test_a_failure_during_the_write_is_caught_too(monkeypatch, tmp_path):
    screen = open_screen(monkeypatch, tmp_path)
    start_a_run(screen)
    monkeypatch.setattr(runner_module, "apply",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AuthenticationError("rejected")))
    screen.button("Upload").click()
    assert "rejected" in screen.text.lower() or "login" in screen.text.lower()


# --- 3. rollback must be reachable --------------------------------------

def test_restore_preview_states_what_goes_back_and_for_how_many_pairs():
    pairs = [("1", "acct_one"), ("2", "acct_one"), ("3", "acct_two")]
    snapshot = {("1", "acct_one"): (200, {"fixedPrices": [
                    {"tradePolicyId": "1", "value": 5}]}),
                ("2", "acct_one"): (200, {"fixedPrices": [
                    {"tradePolicyId": "1", "value": 6}]}),
                ("3", "acct_two"): (429, None)}
    sentence = ui.restore_preview(pairs, snapshot)
    assert "2 of 3" in sentence
    assert "1" in sentence


def test_restore_preview_counts_a_prior_of_no_fixed_price_as_restorable():
    """A 200 read of an empty array is a saved copy, not a missing one.

    The preview must agree with what restore will actually do, or the operator
    is told two pairs will be put back and then sees three restored.
    """
    pairs = [("1", "acct_one"), ("2", "acct_one"), ("3", "acct_two")]
    snapshot = {("1", "acct_one"): (200, {"fixedPrices": [
                    {"tradePolicyId": "1", "value": 5}]}),
                ("2", "acct_one"): (200, {"fixedPrices": []}),
                ("3", "acct_two"): (404, None)}
    sentence = ui.restore_preview(pairs, snapshot)
    assert "2 of 3" in sentence


def say_into(collected):
    return lambda text: collected.append(str(text))


def test_run_restore_says_skipped_pairs_are_still_holding_the_new_prices():
    """The live failure was a report the operator could not act on.

    "restored 0 - skipped 18" reads as "the undo was a no-op". The truth was
    that all 18 new prices were still live in VTEX. A skipped pair has to say
    what it means for production, not just that it was counted.
    """
    from vtex_fixed_price_uploader.restore import RestoreResult

    pairs = [(str(n), "acct_one") for n in range(18)]
    said = []
    ui.run_restore(a_config(), {}, pairs, "tok", object(), say_into(said),
                   restore_fn=lambda *a, **k: RestoreResult(skipped=18))
    text = " ".join(said).lower()
    assert "18" in text
    assert "still" in text and ("live" in text or "in vtex" in text)
    assert "upload" in text, "it must name what is still live, not just that"
    assert "nothing was put back" in text


def test_run_restore_reports_pairs_it_never_reached_after_a_halt():
    """A halt leaves pairs neither restored nor skipped; they still count.

    Subtracting restored and failed used to fold these into the skipped
    sentence, which told the operator the saved copy was at fault when the
    real cause was the login dying part-way.
    """
    from vtex_fixed_price_uploader.restore import RestoreResult

    pairs = [(str(n), "acct_one") for n in range(10)]
    said = []
    ui.run_restore(a_config(), {}, pairs, "tok", object(), say_into(said),
                   restore_fn=lambda *a, **k: RestoreResult(
                       restored=3, skipped=1, halted="Your login was rejected."))
    text = " ".join(said).lower()
    assert "6" in text
    assert "not reached" in text or "never reached" in text


def test_undo_is_on_the_screen_and_asks_before_writing(monkeypatch, tmp_path):
    screen = open_screen(monkeypatch, tmp_path)
    screen.ui["token"].value = "a-login-value"
    screen.button("Put the previous prices back").click()
    assert screen.calls["restore"] == [], "undo wrote without confirmation"
    assert "put back" in screen.text.lower()


def test_undo_confirmation_runs_the_rollback(monkeypatch, tmp_path):
    screen = open_screen(monkeypatch, tmp_path)
    screen.ui["token"].value = "a-login-value"
    screen.button("Put the previous prices back").click()
    screen.button("Yes, put").click()
    assert screen.calls["restore"] == [[("1001", "acct_one")]]
    assert "2" in screen.text


def test_undo_without_a_login_says_so(monkeypatch, tmp_path):
    screen = open_screen(monkeypatch, tmp_path)
    screen.button("Put the previous prices back").click()
    assert "login" in screen.text.lower()
    assert screen.calls["restore"] == []


# --- 5, 7, 8 ------------------------------------------------------------

def test_the_findings_csv_is_offered_for_download(monkeypatch, tmp_path):
    findings = [Finding(rule="B4", severity="blocking", sku="1008", code="R1",
                        account="acct_one", message="Not in that region.",
                        detail="Remove this region from your file.")]
    screen = open_screen(monkeypatch, tmp_path, findings=findings)
    start_a_run(screen)
    assert 'download="' in screen.text
    assert ".csv" in screen.text


def test_the_full_scope_is_restated_at_the_button(monkeypatch, tmp_path):
    screen = open_screen(monkeypatch, tmp_path)
    start_a_run(screen)
    assert "This will write 2 prices to 2 accounts" in screen.text


def test_build_ui_uses_the_paths_it_is_given(monkeypatch, tmp_path):
    screen = open_screen(monkeypatch, tmp_path, folder=None)
    assert screen.ui["log_path"] == screen.log_path
    assert screen.ui["snapshot_path"] == screen.snapshot_path
    assert screen.calls["logs"] == [screen.log_path]


# --- Fix wave E: the two-minute settle wait must be announced before it -----


def _lines_before_the_read_back(call):
    """Run `call(say)` and return only what was said BEFORE verify ran."""
    said, order = [], []

    def say(line):
        order.append("say")
        said.append(line)

    def verify_fn(*args, **kwargs):
        order.append("verify")
        return verification_result()

    call(say, verify_fn)
    assert "verify" in order, "verify never ran"
    return said[:order.index("verify")]


def _assert_announces_the_wait(lines):
    notice = " ".join(lines).lower()
    assert "two minutes" in notice, notice
    assert ("waiting" in notice or "pausing" in notice), notice
    assert "do not close" in notice, notice


def test_the_settle_wait_is_announced_before_it_starts_on_the_halt_path():
    lines = _lines_before_the_read_back(
        lambda say, verify_fn: ui.apply_and_verify(
            None, SimpleNamespace(), "t", None, say,
            apply_fn=lambda *a, **k: ApplyResult(
                written=349, failed=20,
                halted="Your login was rejected part-way through."),
            verify_fn=verify_fn))
    _assert_announces_the_wait(lines)


def test_the_settle_wait_is_announced_before_it_starts_on_the_clean_path():
    lines = _lines_before_the_read_back(
        lambda say, verify_fn: ui.apply_and_verify(
            None, SimpleNamespace(), "t", None, say,
            apply_fn=lambda *a, **k: ApplyResult(written=5),
            verify_fn=verify_fn))
    _assert_announces_the_wait(lines)


def test_the_settle_wait_is_announced_before_a_recheck():
    lines = _lines_before_the_read_back(
        lambda say, verify_fn: ui.verify_only(
            None, SimpleNamespace(), "t", say, verify_fn=verify_fn))
    _assert_announces_the_wait(lines)
    assert any("nothing is written" in line.lower() for line in lines)


# --- the styled screen: same facts, told so an operator can act on them ---

def all_matched():
    return VerifyResult(matched=2, rows=({"verdict": "match",
                                          "live_entries": 1},
                                         {"verdict": "match",
                                          "live_entries": 1}))


def run_with_show(verify, apply_result=None):
    """Drive apply_and_verify through the styled sink and return the markup."""
    shown = []
    ui.apply_and_verify(
        None, SimpleNamespace(), "t", None, lambda _t, *a, **k: None,
        apply_fn=lambda *a, **k: apply_result or ApplyResult(written=2),
        verify_fn=lambda *a, **k: verify,
        show=shown.append)
    return " ".join(shown)


def test_a_fully_matched_read_back_says_so_in_its_headline():
    assert "Every row confirmed in VTEX" in run_with_show(all_matched())


def test_a_read_back_with_open_questions_never_claims_confirmation():
    markup = run_with_show(verification_result())
    assert "Every row confirmed" not in markup
    assert "need attention" in markup


def test_the_styled_read_back_still_carries_the_full_accounting():
    assert "attempted =" in run_with_show(all_matched())


def test_the_styled_write_result_names_a_failure_in_its_headline():
    markup = run_with_show(all_matched(),
                           ApplyResult(written=2, failed=3))
    assert "failures" in markup.lower()


def test_an_undo_that_skipped_pairs_does_not_read_as_finished_and_fine():
    shown = []
    pairs = [("1", "acct_one"), ("2", "acct_one")]
    ui.run_restore(
        a_config(), {}, pairs, "tok", object(), lambda _t, *a, **k: None,
        restore_fn=lambda *a, **k: SimpleNamespace(
            restored=1, failed=0, skipped=1, halted=""),
        show=shown.append)
    assert "read this" in " ".join(shown).lower()


# --- the undo asks one question, on a screen of its own -----------------

def test_declining_the_undo_writes_nothing_and_says_where_that_leaves_you(
        monkeypatch, tmp_path):
    """A confirmation with no way out is a confirmation people click through.

    Declining also has to state the consequence: the prices from the upload
    are still live. "Nothing happened" is not the same fact.
    """
    screen = open_screen(monkeypatch, tmp_path)
    screen.ui["token"].value = "a-login-value"
    screen.button("Put the previous prices back").click()
    screen.button("No, leave the prices").click()
    assert screen.calls["restore"] == []
    text = screen.text.lower()
    assert "nothing was put back" in text
    assert "still live" in text


def test_the_undo_offer_clears_the_upload_screen_first(monkeypatch, tmp_path):
    """One decision on screen at a time.

    The undo used to be offered into the output the upload screen still held,
    so "Upload N prices" and "Yes, put those prices back" sat there together
    with nothing saying which step each belonged to.
    """
    screen = open_screen(monkeypatch, tmp_path)
    start_a_run(screen)
    before = screen.ui["output"].clears
    screen.ui["token"].value = "a-login-value"
    screen.button("Put the previous prices back").click()
    assert screen.ui["output"].clears > before


def test_answering_the_undo_stands_both_buttons_down(monkeypatch, tmp_path):
    screen = open_screen(monkeypatch, tmp_path)
    screen.ui["token"].value = "a-login-value"
    screen.button("Put the previous prices back").click()
    confirm = screen.button("Yes, put")
    cancel = screen.button("No, leave the prices")
    confirm.click()
    assert confirm.disabled is True
    assert cancel.disabled is True
