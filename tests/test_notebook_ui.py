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


def test_not_ready_before_anything_is_ticked():
    state = AckState(model())
    assert state.ready is False


def test_ready_once_every_group_is_ticked():
    state = AckState(model(ack_keys=("W1-111", "W6-222")))
    state.tick("W1-111")
    assert state.ready is False
    state.tick("W6-222")
    assert state.ready is True


def test_unticking_removes_readiness():
    state = AckState(model())
    state.tick("W1-111")
    state.tick("W1-111", checked=False)
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


def test_blocking_reason_names_what_is_missing():
    state = AckState(model(ack_keys=("W1-111", "W6-222")))
    state.tick("W1-111")
    assert "1" in state.blocking_reason


def test_ticking_an_unknown_key_is_ignored():
    state = AckState(model(ack_keys=("W1-111",)))
    state.tick("nonsense")
    assert state.ready is False


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


def test_interactive_verification_displays_the_accounting_summary():
    source = inspect.getsource(ui.run_interactive)
    assert "verification_summary(check)" in source
